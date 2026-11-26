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

from provrca_bench.framebench.build import FAMILIES, T1_FAMILIES, build_variant
from provrca_bench.framebench.case import CleanCase, load_dev_cases
from provrca_bench.framebench.doses import DOSES
from provrca_bench.framebench.target import checks_as_rows, draw_target


def _one_case(case: CleanCase, seed: int, out: Path, sealed: Path) -> tuple[list[dict], list[dict]]:
    target, checks = draw_target(case, seed)
    rows = []
    jobs = ([("clean", "T2", "high")] + [(f, "T2", d) for f in FAMILIES if f != "clean" for d in DOSES]
            + [(f, "T1", d) for f in sorted(T1_FAMILIES) for d in DOSES])
    for family, threat, dose in jobs:
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
    a = p.parse_args(argv)

    dev = {json.loads(l)["source"].split("/")[0] for l in a.answers.read_text().splitlines()}
    cases = load_dev_cases(dataset_root=a.dataset, tasks_root=a.tasks, answers_jsonl=a.answers,
                           dev_templates=dev)
    if a.cases:
        cases = [c for c in cases if c.case_id in set(a.cases)]
    a.sealed.mkdir(parents=True, exist_ok=True)
    targets, index = [], []
    with ProcessPoolExecutor(max_workers=a.workers) as pool:
        futs = {pool.submit(_one_case, c, a.seed, a.out, a.sealed): c.case_id for c in cases}
        for f in as_completed(futs):
            t, rows = f.result()
            targets += t
            index += rows
            print(f"{futs[f]}: {len(rows)} variants, target "
                  f"{next((r['service'] for r in t if r['drawn']), None)}", flush=True)
    for name, rows in (("targets.csv", targets), ("index.csv", index)):
        rows.sort(key=lambda r: (r["case_id"], r.get("variant", r.get("service", ""))))
        with (a.sealed / name).open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    print(f"done: {len(index)} variants over {len(cases)} cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
