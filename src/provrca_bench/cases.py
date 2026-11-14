"""Build the clean case tree with rebuilt derived files (build-guide P1 item 4).

Appendix A3 fixes the decision this implements: **clean and overlay cases both use rebuilt derived
files.** RCAEval documents that its converter's derived files differ from the released ones (the
released `tracets_err` summed status codes, and the released latency baseline mixed units), so if
overlay cases used rebuilt files and clean cases used released ones, every measured effect would
mix the overlay with the rebuild. Both conditions therefore come from the same `derive` call.

The released files are not discarded: they are kept in `<case>/released/` so P1 item 5 can report
the released-versus-rebuilt difference, which is the only thing they are for.

Data hygiene is mechanical, not a convention. `build_clean_cases` refuses any template that is not
in the development split, before it copies anything, and returns the manifest of what it touched.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: the files `rcaeval-convert derive` regenerates, and therefore the ones that must not be the
#: released versions in a clean case
DERIVED = ("logts.csv", "tracets_err.csv", "tracets_lat.csv", "cluster_info.json")

#: derive also rewrites logs.csv, adding cluster_id and log_template
REWRITTEN = ("logs.csv",)


class HygieneError(RuntimeError):
    """Raised before anything is read when a path is not development data."""


@dataclass
class BuildReport:
    case_id: str
    template: str
    source: Path
    destination: Path
    rebuilt: list[str] = field(default_factory=list)
    released_kept: list[str] = field(default_factory=list)
    inject_time: int | None = None


def dev_templates(dev_cases_json: Path) -> set[str]:
    """The 12 development template names, as `<service>_<fault>`."""
    spec = json.loads(Path(dev_cases_json).read_text())
    templates = {f"{service}_{fault}" for service, fault in spec["templates"]}
    if len(templates) != spec["n_templates"]:
        raise HygieneError(
            f"{dev_cases_json}: {len(templates)} templates derived but n_templates is "
            f"{spec['n_templates']}")
    return templates


def guard(source: Path, allowed: set[str]) -> str:
    """Return the template name, or refuse. Called before the case is opened."""
    source = Path(source).resolve()
    for sealed in ("RE2-TT", "RE3-OB", "RE3-SS", "RE2-SS", "RE1-"):
        if sealed in str(source):
            raise HygieneError(
                f"refused: {source} is in a sealed or unused data set ({sealed}). Only the "
                "development templates may be built.")
    template = source.parent.name if source.name.isdigit() else source.name
    if template not in allowed:
        raise HygieneError(
            f"refused: template {template!r} is not one of the {len(allowed)} development "
            "templates. It is test stratum S1 and must not be opened before the G3 freeze.")
    return template


def build_clean_case(source: Path, destination: Path, template: str,
                     python: str = sys.executable) -> BuildReport:
    """Copy one raw case and rebuild its derived files in place."""
    source, destination = Path(source), Path(destination)
    report = BuildReport(case_id=destination.name, template=template, source=source,
                         destination=destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    for item in sorted(source.iterdir()):
        if item.is_file():
            shutil.copy2(item, destination / item.name)

    inject_time_file = destination / "inject_time.txt"
    if not inject_time_file.exists():
        raise RuntimeError(f"{source}: no inject_time.txt, so derive has no failure start")
    report.inject_time = int(inject_time_file.read_text().strip())

    # keep the released derived files for the parity report, and only for that
    released = destination / "released"
    released.mkdir()
    for name in DERIVED + REWRITTEN:
        current = destination / name
        if current.exists():
            shutil.copy2(current, released / name)
            report.released_kept.append(name)

    run = subprocess.run([python, "-m", "RCAEval.convert", "derive", str(destination)],
                         capture_output=True, text=True)
    if run.returncode != 0:
        raise RuntimeError(f"derive failed for {destination}:\n{run.stderr.strip()}")
    report.rebuilt = [n for n in DERIVED + REWRITTEN if (destination / n).exists()]
    return report


def already_built(destination: Path) -> bool:
    """Whether this case has a complete rebuild: every derived file plus the released copies.

    Checked so a resumed run does not redo work -- building the tree copies about 100 MB per case
    and runs Drain3 over its logs, so an interrupted run must be resumable.
    """
    destination = Path(destination)
    if not (destination / "released").is_dir():
        return False
    return all((destination / name).exists() for name in DERIVED + REWRITTEN)


def build_clean_cases(*, answers_jsonl: Path, dev_cases_json: Path, raw_root: Path,
                      out_root: Path, python: str = sys.executable,
                      skip_existing: bool = True, progress=None) -> list[BuildReport]:
    """Build every development case into `out_root/<case_id>/`.

    `skip_existing` resumes an interrupted run; `progress` is called with each report as it is
    finished, so a long run reports as it goes rather than only at the end.
    """
    allowed = dev_templates(Path(dev_cases_json))
    wanted = set(json.loads(Path(dev_cases_json).read_text())["cases"])
    reports = []
    with Path(answers_jsonl).open() as f:
        for line in f:
            if not line.strip():
                continue
            answer = json.loads(line)
            if answer["case_id"] not in wanted:
                continue
            source = Path(raw_root) / answer["source"]
            template = guard(source, allowed)
            destination = Path(out_root) / answer["case_id"]
            if skip_existing and already_built(destination):
                report = BuildReport(case_id=answer["case_id"], template=template, source=source,
                                     destination=destination,
                                     rebuilt=[n for n in DERIVED + REWRITTEN
                                              if (destination / n).exists()],
                                     released_kept=sorted(
                                         p.name for p in (destination / "released").iterdir()),
                                     inject_time=int((destination / "inject_time.txt")
                                                     .read_text().strip()))
            else:
                report = build_clean_case(source, destination, template, python=python)
            reports.append(report)
            if progress is not None:
                progress(report)
    if len(reports) != len(wanted):
        raise RuntimeError(f"built {len(reports)} cases but the development split names "
                           f"{len(wanted)}")
    return reports


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="P1 item 4: build the clean case tree")
    ap.add_argument("--answers", required=True, help="re2-ob.answers.jsonl from the export")
    ap.add_argument("--dev-cases", required=True, help="dev_cases.json, the seeded draw")
    ap.add_argument("--raw-root", required=True, help="data/RE2/RE2-OB")
    ap.add_argument("--out", required=True, help="cases/clean")
    args = ap.parse_args(argv)
    def show(r: BuildReport) -> None:
        print(f"  {r.case_id}  {r.template:28s} rebuilt {len(r.rebuilt)} files  "
              f"inject_time {r.inject_time}", flush=True)

    reports = build_clean_cases(answers_jsonl=Path(args.answers),
                                dev_cases_json=Path(args.dev_cases),
                                raw_root=Path(args.raw_root), out_root=Path(args.out),
                                progress=show)
    print(f"built {len(reports)} clean cases into {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
