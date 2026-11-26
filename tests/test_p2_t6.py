"""P2-T6 (construction) for the families built so far, plus the label check the step-4 review asked
for: the generator's labels are compared with the chain's verdicts on the overlay. This is a
*test*, never a label source; the labels come from what the generator did.

* F3c, F2c, FSc, F2cD (T2): every forged span is compatible or unknown under D4 -- never
  contradicted or rejected; every F2c/FSc forged span is NO_WITNESS (the witness link is cut).
* T1-F2, T1-F3: with attribution off every forged server span is compatible (the forged pair
  confirms itself); with attribution on every forged record is rejected.
* Labels: for every labelled claim, ``rule_applicable`` equals "the targeted rule was applied"
  in the overlay judged with attribution off.

Runs on every 4th
development case (``PROVRCA_FULL=1`` for all 36).
"""
from __future__ import annotations

import csv
import json
import os
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

from provrca_bench.provrca.chain import judge_spans, read_traces
from provrca_bench.provrca.rules import Config

REPO = Path(__file__).resolve().parents[1]
GEN = Path(os.environ.get("PROVRCA_FRAMEBENCH_ROOT", REPO / "cases" / "framebench-dev"))
SEALED = Path(os.environ.get("PROVRCA_FRAMEBENCH_SEALED", REPO / "sealed" / "framebench-dev"))
INDEX = SEALED / "index.csv"
WORKERS = int(os.environ.get("PROVRCA_WORKERS", "6"))
APPLIED = {"r1": ("OK_ENCLOSED", "R1_ENCLOSURE_FAIL"), "r2": ("OK_STATUS", "R2_STATUS_CONFLICT")}
RULES = {"F2": ("r2",), "F3": ("r1",), "F3c": ("r1",), "FS": ("r1", "r2"),
         "F2c": ("r2",), "FSc": ("r1", "r2"), "F2cD": ("r2",)}
ADAPTIVE = {"F3c", "F2c", "FSc", "F2cD"}


def _rows():
    if not INDEX.exists():
        return []
    rows = [r for r in csv.DictReader(INDEX.open()) if r.get("threat")]
    cases = sorted({r["case_id"] for r in rows})
    keep = set(cases if os.environ.get("PROVRCA_FULL") else cases[::4])
    return [r for r in rows if r["case_id"] in keep]


ROWS = _rows()
real = pytest.mark.skipif(not ROWS, reason="no FrameBench development set with T1/F3c variants")


def _check(row: dict) -> tuple[str, list[str]]:
    cfg = Config.load()
    v = row["variant"]
    labels = {json.loads(l)["record_id"]: json.loads(l)
              for l in (SEALED / row["case_id"] / f"{v}.labels.jsonl").read_text().splitlines()}
    spans = {k: l for k, l in labels.items() if k.startswith("span:")}
    if not spans:
        return v, []
    t = read_traces(GEN / row["case_id"] / v / "traces.csv")
    off = judge_spans(t, cfg, attribution=False)
    off = off[off.record_id.isin(spans)]
    problems = []
    for r in off.itertuples():
        lab = spans[r.record_id]
        if lab["escape"] == "witness":
            continue
        applied = any(getattr(r, f"{k}_reason") in APPLIED[k] for k in RULES[row["family"]])
        if lab["rule_applicable"] != applied:
            problems.append(f"{r.record_id}: label {lab['rule_applicable']} but applied={applied}")
    if row["family"] in ADAPTIVE or row["threat"] == "T1":
        on = judge_spans(t, cfg, attribution=True)
        on = on[on.record_id.isin(spans)]
        if row["threat"] == "T1":
            claims = off[[spans[i]["escape"] != "witness" for i in off.record_id]]
            if not (claims.verdict == "compatible").all():
                problems.append(f"T1 without attribution: {Counter(claims.verdict)}")
            if not (on.verdict == "rejected").all():
                problems.append(f"T1 with attribution: {Counter(on.verdict)}")
        else:
            if on.verdict.isin(["contradicted", "rejected"]).any():
                problems.append(f"{row['family']}: {Counter(on.verdict)}")
            if row["family"] in ("F2c", "FSc") and not (on.reason == "NO_WITNESS").all():
                problems.append(f"{row['family']} not unlinked: {Counter(on.reason)}")
    return v, problems[:5]


@real
def test_p2_t6_constructions_and_labels():
    todo = [r for r in ROWS if r["family"] in RULES]
    with ProcessPoolExecutor(WORKERS) as pool:
        res = {f"{r['case_id']}/{v}": p for r, (v, p) in zip(todo, pool.map(_check, todo))}
    assert {k: v for k, v in res.items() if v} == {}
    fams = {k.split("/")[1].split("-")[0] for k in res}
    assert any("-T1-" in k for k in res) and ADAPTIVE <= fams
