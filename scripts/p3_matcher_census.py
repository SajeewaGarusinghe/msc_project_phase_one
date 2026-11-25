"""Per-case outcome census of the P3 matcher on clean development data (RE2-OB dev, RE3-TT).

    uv run python scripts/p3_matcher_census.py --out evidence/p3_matcher_census.csv

One row per (dataset, case, role, outcome, detail, tier). Development data only: RE2-OB cases come
from parity/dataset-rebuilt (the 12 development templates), RE3-TT from PROVRCA_RE3TT_ROOT.
"""
from __future__ import annotations

import argparse
import csv
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_p3_matcher import _re2_cases, _re3_cases  # noqa: E402

from provrca_bench.provrca.matcher import match, read_spans  # noqa: E402


def one(item):
    (dataset, cid), path = item
    o = match(read_spans(path / "traces.csv"))
    g = o.groupby(["role", "outcome", "detail", "tier"]).size()
    return [{"dataset": dataset, "case_id": cid, "role": r, "outcome": oc, "detail": d,
             "tier": t, "n": int(n)} for (r, oc, d, t), n in g.items()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    items = [(("re2-ob-dev", k), v) for k, v in _re2_cases().items()] + \
            [(("re3-tt", k), v) for k, v in _re3_cases().items()]
    rows = []
    with ProcessPoolExecutor(a.workers) as pool:
        for r in pool.map(one, items):
            rows += r
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: tuple(str(v) for v in r.values())))
    print(f"{len(items)} cases, {len(rows)} rows -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
