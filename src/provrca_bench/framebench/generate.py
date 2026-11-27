"""Generate the FrameBench **development** set (build-guide P2 item 7). Test sets wait for G3.

    PROVRCA_HARNESS_PYTHON=~/g0_pilot/.venv/bin/python \\
    uv run python -m provrca_bench.framebench.generate \\
        --dataset parity/dataset-rebuilt --tasks parity/tasks-rebuilt/re2-ob/cases \\
        --answers parity/tasks-rebuilt/re2-ob.answers.jsonl \\
        --out cases/framebench-dev --sealed sealed/framebench-dev --seed 20261125 --workers 4

Per case: the target is drawn once (seeded) and shared by every family and dose, so variants of one
case differ only in what was forged. Output: ``<out>/<case>/<variant>/`` and, sealed,
``<sealed>/<case>/<variant>.{labels.jsonl,meta.json}``, ``<sealed>/targets.csv`` and
``<sealed>/index.csv``. The index names families, so it is sealed too.
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from provrca_bench.framebench.build import FAMILIES, HONEST, T1_FAMILIES, build_variant
from provrca_bench.framebench.case import CleanCase, load_dev_cases
from provrca_bench.framebench.doses import DOSES
from provrca_bench.framebench.target import checks_as_rows, draw_target


def jobs_for(families: set[str] | None = None) -> list[tuple[str, str, str]]:
    """(family, threat, level) for every variant built per case; the honest faults use their own
    levels (AF1: the sign of the clock offset; AF2: one level), not the attack doses. ``families``
    holds family names (every threat) or ``family:threat`` tokens (``F2:T1``)."""
    jobs = ([("clean", "T2", "high")]
            + [(f, "T2", d) for f in FAMILIES if f != "clean"
               for d in (HONEST[f] if f in HONEST else DOSES)]
            + [(f, "T1", d) for f in sorted(T1_FAMILIES) for d in DOSES])
    return [j for j in jobs
            if families is None or j[0] in families or f"{j[0]}:{j[1]}" in families]


def _one_case(case: CleanCase, seed: int, out: Path, sealed: Path,
              families: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    target, checks = draw_target(case, seed)
    rows = []
    for family, threat, dose in jobs_for(families):
        if target is None and family != "clean":
            continue
        t0 = time.time()
        r = build_variant(case, target=target, family=family, threat=threat, dose_level=dose, seed=seed,
                          out_root=out, sealed_root=sealed, checks=checks)
        rows.append({"case_id": case.case_id, "variant": r.variant, "family": family, "threat": threat,
                     "dose": dose if family != "clean" else "", "target": target or "",
                     "edited_spans": r.edited, "injected_lines": r.injected,
                     "labels": len(r.labels),
                     "f3_factor": r.dose.f3_factor if r.dose else "",
                     "achieved_p95_ratio": r.extra.get("achieved_p95_ratio", ""),
                     "achieved_post_nonok_share": r.extra.get("achieved_post_nonok_share", ""),
                     "t1_written": r.extra.get("t1_written", ""),
                     "t1_rc_volume_ratio": (r.extra.get("t1_volume_ratio") or {}).get(case.root_cause, "")
                     if threat == "T1" else "",
                     "af2_b": r.extra.get("af2_b") or "",
                     "seconds": round(time.time() - t0, 1)})
    return checks_as_rows(case, checks, target), rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--tasks", type=Path, required=True)
    p.add_argument("--answers", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--sealed", type=Path, required=True)
    p.add_argument("--seed", type=int, default=20261125)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--cases", nargs="*", help="restrict to these case ids")
    p.add_argument("--families", nargs="*", help="build only these families (or family:threat "
                   "tokens, e.g. F2:T1); the existing "
                   "index.csv is merged (rows of rebuilt variants replaced) and targets.csv is left "
                   "as it is, since the target draw does not depend on the family")
    a = p.parse_args(argv)

    dev = {json.loads(l)["source"].split("/")[0] for l in a.answers.read_text().splitlines()}
    cases = load_dev_cases(dataset_root=a.dataset, tasks_root=a.tasks, answers_jsonl=a.answers,
                           dev_templates=dev)
    if a.cases:
        cases = [c for c in cases if c.case_id in set(a.cases)]
    a.sealed.mkdir(parents=True, exist_ok=True)
    targets, index = [], []
    with ProcessPoolExecutor(max_workers=a.workers) as pool:
        fams = set(a.families) if a.families else None
        futs = {pool.submit(_one_case, c, a.seed, a.out, a.sealed, fams): c.case_id for c in cases}
        for f in as_completed(futs):
            t, rows = f.result()
            targets += t
            index += rows
            print(f"{futs[f]}: {len(rows)} variants, target "
                  f"{next((r['service'] for r in t if r['drawn']), None)}", flush=True)
    outputs = [("targets.csv", targets), ("index.csv", index)]
    if a.families:
        old = list(csv.DictReader((a.sealed / "index.csv").open())) if (a.sealed / "index.csv").exists() else []
        new_keys = {(r["case_id"], r["variant"]) for r in index}
        cols = list(dict.fromkeys([*(old[0] if old else {}), *index[0]]))
        index = [{c: r.get(c, "") for c in cols} for r in old
                 if (r["case_id"], r["variant"]) not in new_keys] + \
                [{c: r.get(c, "") for c in cols} for r in index]
        outputs = [("index.csv", index)]
    for name, rows in outputs:
        rows.sort(key=lambda r: (r["case_id"], r.get("variant", r.get("service", ""))))
        with (a.sealed / name).open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    print(f"done: {len(index)} variants over {len(cases)} cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
