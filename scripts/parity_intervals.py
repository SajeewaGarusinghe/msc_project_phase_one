"""P1-T3: bootstrap 95% intervals for the clean development AC@1 of BARO and the reference agent.

Two resampling units, both reported: the case (36), and the fault template (12) -- the pilot's
clustering, because two development root-cause services cannot form clusters (K14). Scorer-side
only (A4): it reads the answers file.

    uv run python scripts/parity_intervals.py \
        --answers parity/tasks-rebuilt/re2-ob.answers.jsonl \
        --agent parity/preds-rebuilt.jsonl --baro runs/parity/baro_parity.csv \
        --out evidence/p1_t3_parity.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

SEED, B = 20261125, 10_000


def interval(frame: pd.DataFrame, col: str, cluster: str | None, rng) -> list[float]:
    if cluster is None:
        x = frame[col].to_numpy(float)
        draws = [x[rng.integers(0, len(x), len(x))].mean() for _ in range(B)]
    else:
        g = frame.groupby(cluster)[col].agg(["sum", "count"]).to_numpy(float)
        draws = []
        for _ in range(B):
            i = rng.integers(0, len(g), len(g))
            draws.append(g[i, 0].sum() / g[i, 1].sum())
    return [round(float(v), 4) for v in np.percentile(draws, [2.5, 97.5])]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--answers", type=Path, required=True)
    p.add_argument("--agent", type=Path, required=True)
    p.add_argument("--baro", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()

    answers = {json.loads(l)["case_id"]: json.loads(l) for l in a.answers.read_text().splitlines()}
    preds = {json.loads(l)["case_id"]: json.loads(l) for l in a.agent.read_text().splitlines()}
    agent = pd.DataFrame([{
        "case_id": cid,
        "template": ans["source"].split("/")[0],
        "ac1": int(bool(preds.get(cid, {}).get("ranks"))
                   and preds[cid]["ranks"][0].split("_")[0] == ans["service"]),
    } for cid, ans in answers.items()])

    baro = pd.read_csv(a.baro)
    baro = baro[baro.flat_metrics_dropped]
    baro["ac1"] = baro["ac1"].astype(int)

    rng = np.random.default_rng(SEED)
    out = {"test": "P1-T3", "seed": SEED, "resamples": B, "cases": len(answers),
           "missing_agent_predictions": sorted(set(answers) - set(preds)), "arms": {}}
    for name, frame in [("agent_rebuilt", agent),
                        ("baro_rebuilt", baro[baro.condition == "rebuilt"]),
                        ("baro_released", baro[baro.condition == "released"])]:
        out["arms"][name] = {
            "ac1": round(float(frame["ac1"].mean()), 4),
            "ci95_case": interval(frame, "ac1", None, rng),
            "ci95_template": interval(frame, "ac1", "template", rng),
            "n": int(len(frame)),
        }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out["arms"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
