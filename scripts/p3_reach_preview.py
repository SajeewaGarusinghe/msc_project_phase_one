"""Development-data preview of R1/R2 reach on FrameBench overlays (T2, F3c, T1). NOT a result.

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
FAMILIES = ("F2", "F3", "FS", "F3c")


def one(case_id: str) -> list[dict]:
    cfg = Config.load()
    rows = []
    for v in sorted(p.name for p in (GEN / case_id).iterdir()):
        fam, threat, dose = (v.split("-") + ["", "", ""])[:3]
        if fam not in FAMILIES:
            continue
        labels = [json.loads(l) for l in (SEALED / case_id / f"{v}.labels.jsonl").read_text().splitlines()]
        claims = {l["record_id"]: l for l in labels if l["escape"] != "witness"}
        t = read_traces(GEN / case_id / v / "traces.csv")
        for attribution in ((True, False) if threat == "T1" else (True,)):
            j = judge_spans(t, cfg, attribution=attribution)
            is_claim = j.record_id.isin(claims)
            esc = j.record_id.map(lambda r: claims[r]["escape"] if r in claims else "")
            c = Counter(zip(is_claim, j.verdict, j.reason, j.r1_reason, j.r2_reason, esc))
            for (f, verdict, reason, r1, r2, e), n in c.items():
                if not f and verdict not in ("contradicted", "rejected"):
                    continue          # genuine records and witnesses: only demotions matter
                rows.append({"case_id": case_id, "family": fam, "threat": threat, "dose": dose,
                             "attribution": attribution,
                             "record": "forged_claim" if f else "other", "verdict": verdict,
                             "reason": reason, "r1": r1, "r2": r2, "escape": e, "n": n})
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
