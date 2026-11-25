"""R1/R2 on every clean development pair (P3-T2 evidence): per case, route, tier and each rule's
reason. Development data only (RE2-OB dev rebuilt, RE3-TT).

    uv run python scripts/p3_rules_clean.py --out evidence/p3_rules_clean.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_p3_rules import _clean_dirs  # noqa: E402

from provrca_bench.provrca.chain import judge_spans, read_traces  # noqa: E402
from provrca_bench.provrca.rules import Config  # noqa: E402


def one(path: Path) -> list[dict]:
    j = judge_spans(read_traces(path / "traces.csv", with_sender=False), Config.load(),
                    attribution=False)
    s = j[j.role == "server"]
    ds = "re3-tt" if "RE3-TT" in str(path) else "re2-ob-dev"
    case = "/".join(path.parts[-2:])
    c = Counter(zip(s.route.where(s.route != "", "(unmatched or unlisted)"), s.tier, s.verdict,
                    s.reason, s.r1_reason, s.r1_detail, s.r2_reason, s.r2_detail))
    return [{"dataset": ds, "case": case, "route": k[0], "tier": k[1], "verdict": k[2],
             "reason": k[3], "r1": k[4], "r1_detail": k[5], "r2": k[6], "r2_detail": k[7], "n": n}
            for k, n in c.items()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    rows = []
    with ProcessPoolExecutor(a.workers) as pool:
        for r in pool.map(one, _clean_dirs()):
            rows += r
    with a.out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"table sha256 {Config.load().sha256}; {len(rows)} rows -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
