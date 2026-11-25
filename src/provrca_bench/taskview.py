"""One case directory -> one task-view directory, laid out exactly as ``rcaeval-agent export`` lays
out each case (``RCAEval/agent/export.py``: ``EXTRA_FILES``, the primary metrics file, task.json).

``export`` works on a whole dataset; FrameBench variants and demoted views are single cases, so this
places one case's files the same way and takes ``task.json`` **verbatim** from the clean case's task
view (its fields come from the case's metrics and inject time, which overlays never change, K23).
The output directory's name is the caller's: A4 wants it hashed, never a family or dose.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from RCAEval.agent.export import EXTRA_FILES


def make_task_view(case_dir: Path, clean_task_json: Path, out: Path) -> Path:
    case_dir, out = Path(case_dir), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    primary = next((case_dir / n for n in ("data.csv", "simple_metrics.csv") if (case_dir / n).exists()))
    pairs = {"metrics.csv": primary} | {name: case_dir / src for name, src in EXTRA_FILES.items()}
    for name, src in pairs.items():
        if src.exists():
            dst = out / name
            if dst.exists():
                dst.unlink()
            try:
                os.link(src, dst)
            except OSError:
                shutil.copyfile(src, dst)
    shutil.copyfile(clean_task_json, out / "task.json")
    return out
