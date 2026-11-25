"""Real-data counterpart of proposal Fig 13: one trace_summary call on the target of a FrameBench
dev overlay under D0, D4-M2, D4-M2-silent and D4-M1 (development data; P3 step 3 evidence).

    PROVRCA_HARNESS_PYTHON=~/g0_pilot/.venv/bin/python \\
    uv run python scripts/p3_fig13_examples.py --case re2-ob-0001 --out evidence/fig13.json
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from RCAEval.agent.toolkit import CaseToolkit

from provrca_bench.provrca.demotion import DEMOTES, ProvRCALayer, build_view, case_records, drop_masks
from provrca_bench.provrca.rules import Config
from provrca_bench.taskview import make_task_view

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="re2-ob-0001")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    py = os.environ.get("PROVRCA_HARNESS_PYTHON", str(Path.home() / "g0_pilot/.venv/bin/python"))
    task_json = REPO / "parity/tasks-rebuilt/re2-ob/cases" / a.case / "task.json"
    task = json.loads(task_json.read_text())
    cfg, res = Config.load(), {"table_sha256": Config.load().sha256, "case": a.case, "calls": []}
    with tempfile.TemporaryDirectory() as tmp:
        for var in ("F2-T2-high-20261125", "F3-T2-high-20261125"):
            tv = make_task_view(REPO / "cases/framebench-dev" / a.case / var, task_json, Path(tmp) / var[:2])
            rec = case_records(tv, cfg)
            ds, dl = drop_masks(rec, DEMOTES["D4"])
            view = build_view(tv, Path(tmp) / "views", drop_spans=ds, drop_logs=dl, python=py)
            target = rec[rec.verdict == "contradicted"].service.mode().iloc[0]
            args = {"service": target, "top_k": 3}
            raw, dem = CaseToolkit(str(tv)), CaseToolkit(str(view))
            entry = {"variant": var, "tool": "trace_summary", "args": args,
                     "D0": raw.call("trace_summary", args)}
            for mode in ("M2", "M2-silent", "M1"):
                kit = raw if mode == "M1" else dem
                L = ProvRCALayer(arm="D4", mode=mode, records=rec, task=task)
                entry[f"D4-{mode}"] = L.filter("trace_summary", args, kit.call("trace_summary", args), None)[0]
            res["calls"].append(entry)
    a.out.write_text(json.dumps(res, indent=1))
    print(f"-> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
