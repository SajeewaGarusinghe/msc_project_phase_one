"""Harness parity: BARO on the clean development cases, released against rebuilt (P1 item 5).

Appendix A3 decides that clean and overlay cases both use **rebuilt** derived files, so that no
measured effect mixes the overlay with the rebuild. P1 item 5 then has to say how big the rebuild
difference actually is, and BARO is the right instrument for the free half of that: the
multi-source variant reads `logts`, `tracets_err` and `tracets_lat`, which is exactly where the
released and rebuilt files differ, and it uses no language model, so the measurement costs nothing.

Two outcomes per case, both as RCAEval defines them:

  * **AC@1** -- the top-ranked column belongs to the root-cause service;
  * **Inspected@root** -- the 1-based rank of the first column belonging to it, i.e. how far an
    engineer would read before reaching the true cause.

A ranked column is attributed to a service by the text before its first underscore, which is the
same convention that makes an underscore in a service name illegal (A3).

The root-cause labels come from the export's answers file. That is label data, so this module is
**scorer-side only** (A4): nothing here may run inside an arm.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path

import pandas as pd

DERIVED = ("logts", "tracets_err", "tracets_lat")


@dataclass
class ParityRow:
    case_id: str
    template: str
    root_cause: str
    condition: str                 # "rebuilt" or "released"
    flat_metrics_dropped: bool
    ac1: bool
    inspected_at_root: int | None  # None when no ranked column belongs to the root cause
    n_ranked: int
    top1: str


def service_of(column: str) -> str:
    """RCAEval's convention: the service is the text before the first underscore."""
    return column.split("_")[0]


def _read(case: Path, name: str, condition: str) -> pd.DataFrame | None:
    path = case / f"{name}.csv" if condition == "rebuilt" else case / "released" / f"{name}.csv"
    if not path.exists():
        return None
    return pd.read_csv(path)


def drop_flat(metric: pd.DataFrame, inject_time: int, min_iqr: float = 0.0) -> pd.DataFrame:
    """Drop metric columns whose pre-fault interquartile range is zero.

    Step 8 of the G0 evidence found why this is needed: a column that never moves before the
    fault has no scale, so a robust z-score against it is unbounded. `redis_diskio` is the clear
    case -- step 8 measured z = 131072 for it and skipped such columns. Left in, it tops the
    ranking in most cases and the measured accuracy says more about one flat counter than about
    the method.
    """
    normal = metric[metric["time"] < inject_time]
    keep = ["time"]
    for col in metric.columns:
        if col == "time":
            continue
        values = pd.to_numeric(normal[col], errors="coerce").dropna()
        if len(values) > 1 and (values.quantile(0.75) - values.quantile(0.25)) > min_iqr:
            keep.append(col)
    return metric[keep]


def run_baro(case: Path, condition: str, dataset: str = "RE2-OB",
             drop_flat_metrics: bool = False) -> list[str]:
    """`mmbaro`'s ranking for one case under one condition."""
    from RCAEval.e2e.baro import mmbaro

    case = Path(case)
    inject_time = int((case / "inject_time.txt").read_text().strip())
    metric = pd.read_csv(case / "simple_metrics.csv")
    if drop_flat_metrics:
        metric = drop_flat(metric, inject_time)
    data = {"metric": metric}
    for name in DERIVED:
        frame = _read(case, name, condition)
        if frame is not None:
            data[name] = frame
    if "logts" not in data:
        raise RuntimeError(f"{case}: no logts.csv for condition {condition!r}")
    return mmbaro(data=data, inject_time=inject_time, dataset=dataset)["ranks"]


def score(ranks: list[str], root_cause: str) -> tuple[bool, int | None, str]:
    services = [service_of(c) for c in ranks]
    ac1 = bool(services) and services[0] == root_cause
    inspected = services.index(root_cause) + 1 if root_cause in services else None
    return ac1, inspected, (ranks[0] if ranks else "")


def root_causes(answers_jsonl: Path) -> dict[str, tuple[str, str]]:
    """case id -> (root-cause service, template). Label data: scorer only."""
    out = {}
    with Path(answers_jsonl).open() as f:
        for line in f:
            if line.strip():
                a = json.loads(line)
                out[a["case_id"]] = (a["service"], a["source"].split("/")[0])
    return out


def run(*, cases_root: Path, answers_jsonl: Path, out_csv: Path,
        dataset: str = "RE2-OB", progress=None) -> pd.DataFrame:
    """Every case under both conditions, each with and without the flat-metric guard."""
    labels = root_causes(Path(answers_jsonl))
    rows: list[ParityRow] = []
    for case in sorted(p for p in Path(cases_root).iterdir() if p.is_dir()):
        if case.name not in labels:
            raise RuntimeError(f"{case.name} has no label in the answers file")
        root_cause, template = labels[case.name]
        for condition in ("rebuilt", "released"):
          for drop in (False, True):
            ranks = run_baro(case, condition, dataset=dataset, drop_flat_metrics=drop)
            ac1, inspected, top1 = score(ranks, root_cause)
            row = ParityRow(case_id=case.name, template=template, root_cause=root_cause,
                            condition=condition, flat_metrics_dropped=drop, ac1=ac1,
                            inspected_at_root=inspected, n_ranked=len(ranks), top1=top1)
            rows.append(row)
            if progress is not None:
                progress(row)
    frame = pd.DataFrame([asdict(r) for r in rows])
    Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(out_csv, index=False)
    return frame


def summarise(frame: pd.DataFrame) -> pd.DataFrame:
    """AC@1 and median Inspected@root per condition."""
    return (frame.groupby(["condition", "flat_metrics_dropped"])
            .agg(cases=("case_id", "nunique"), ac1=("ac1", "mean"),
                 inspected_median=("inspected_at_root", "median"),
                 inspected_missing=("inspected_at_root", lambda s: int(s.isna().sum())),
                 ranked_columns_median=("n_ranked", "median"))
            .reset_index())


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="P1 item 5: BARO parity, released against rebuilt")
    ap.add_argument("--cases", required=True)
    ap.add_argument("--answers", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dataset", default="RE2-OB")
    args = ap.parse_args(argv)

    def show(r: ParityRow) -> None:
        print(f"  {r.case_id} {r.condition:9s} flat_dropped={int(r.flat_metrics_dropped)} AC@1={int(r.ac1)} "
              f"inspected={r.inspected_at_root} of {r.n_ranked}  top1={r.top1}", flush=True)

    frame = run(cases_root=Path(args.cases), answers_jsonl=Path(args.answers),
                out_csv=Path(args.out), dataset=args.dataset, progress=show)
    print()
    print(summarise(frame).to_string(index=False))
    print(f"\nwrote {args.out}")
    print("ALL_DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
