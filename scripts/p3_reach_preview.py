"""Development-data preview of R1/R2 reach on FrameBench T2 overlays (P3 step 2). NOT a result.

    uv run python scripts/p3_reach_preview.py --out evidence/p3_reach_preview.csv

Run only after the applicability table was committed and hashed: reach must never be used to
tune the table (proposal §08: argue, then hunt; only a clean counter-example changes a route).
Sealed labels are read here, scorer-side, to tell forged records from genuine ones.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from provrca_bench.provrca.chain import judge_spans, read_traces
from provrca_bench.provrca.rules import Config

REPO = Path(__file__).resolve().parents[1]
GEN = Path(os.environ.get("PROVRCA_FRAMEBENCH_ROOT", REPO / "cases" / "framebench-dev"))
SEALED = Path(os.environ.get("PROVRCA_FRAMEBENCH_SEALED", REPO / "sealed" / "framebench-dev"))
FAMILIES = ("F2", "F3", "FS")


def one(case_id: str) -> list[dict]:
    cfg = Config.load()
    rows = []
    for fam in FAMILIES:
        for dose in ("high", "low"):
            v = f"{fam}-T2-{dose}-20261125"
            if not (GEN / case_id / v).exists():
                continue
            forged = {json.loads(l)["record_id"] for l in
                      (SEALED / case_id / f"{v}.labels.jsonl").read_text().splitlines()}
            j = judge_spans(read_traces(GEN / case_id / v / "traces.csv"), cfg)
            is_forged = j.record_id.isin(forged)
            c = Counter(zip(is_forged, j.verdict, j.reason, j.r1_reason, j.r2_reason, j.tier))
            for (f, verdict, reason, r1, r2, tier), n in c.items():
                if not f and verdict != "contradicted":
                    continue          # genuine records: only collateral contradictions matter
                rows.append({"case_id": case_id, "family": fam, "dose": dose,
                             "record": "forged" if f else "genuine", "verdict": verdict,
                             "reason": reason, "r1": r1, "r2": r2, "tier": tier, "n": n})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    cases = sorted(p.name for p in GEN.iterdir() if p.is_dir())
    rows = []
    with ProcessPoolExecutor(a.workers) as pool:
        for r in pool.map(one, cases):
            rows += r
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"table sha256 {Config.load().sha256}; {len(cases)} cases, {len(rows)} rows -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
