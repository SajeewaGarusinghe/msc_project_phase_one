"""P3 step 5: the honest faults AF1 (clock skew) and AF2 (service mislabelling), P3-T3, P3-T4, and
the named P3-T5 run.

Unit layer (CI): the builders on synthetic rows. Real-data layer: the development set's AF
variants judged by the chain with attribution on and off, via ``scripts/p3_honest_faults.py``
(every 4th case; ``PROVRCA_FULL=1`` for all 36), and P3-T5 as P2-T6's construction checks over
every adaptive and T1 variant.

* P3-T3 (Skew): AF1 changes no verdict in the decision chain, in either mode. This holds by
  construction (no rule reads a timestamp) and is reported as such.
* P3-T4 (Mislabel): with attribution on, every relabelled span is rejected (ATTR_MISMATCH);
  with it off, none is contradicted, so the catch is attribution's, not R1's or R2's; no genuine
  record is contradicted in either mode.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from provrca_bench.framebench import build as fb
from provrca_bench.framebench import honest
from provrca_bench.framebench.case import CleanCase

REPO = Path(__file__).resolve().parents[1]
GEN = Path(os.environ.get("PROVRCA_FRAMEBENCH_ROOT", REPO / "cases" / "framebench-dev"))
SEALED = Path(os.environ.get("PROVRCA_FRAMEBENCH_SEALED", REPO / "sealed" / "framebench-dev"))
DATASET = REPO / "parity" / "dataset-rebuilt"
TASKS = REPO / "parity" / "tasks-rebuilt" / "re2-ob" / "cases"
ANSWERS = REPO / "parity" / "tasks-rebuilt" / "re2-ob.answers.jsonl"
WORKERS = int(os.environ.get("PROVRCA_WORKERS", "6"))


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # so worker processes can pickle its functions by name
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------------------------
# Unit layer
# --------------------------------------------------------------------------------------------

HEADER = ("time,traceID,spanID,serviceName,methodName,operationName,startTimeMillis,"
          "startTime,duration,statusCode,parentSpanID\n")


def test_writer_moves_time_keeps_duration_and_the_sender_is_the_clean_writer(tmp_path):
    src = tmp_path / "in.csv"
    src.write_text(HEADER + "18:35,t1,s1,frontendservice,GetCart,op,1000,1000000,10,0.0,p\n"
                            "18:35,t1,s2,cartservice,GetCart,op,1000,1000001,7,0.0,s1\n")
    dst = tmp_path / "out.csv"
    fb._write_traces(src, dst, {"s2": {"startTime": "1250001", "startTimeMillis": "1250",
                                       "time": "18:35", "_svc": "cartservice"}})
    assert dst.read_text().splitlines()[2] == \
        "18:35,t1,s2,cartservice,GetCart,op,1250,1250001,7,0.0,s1,cartservice"
    fb._write_traces(src, dst, {"s2": {"serviceName": "adservice", "_svc": "cartservice"}})
    assert dst.read_text().splitlines()[2].endswith(",s1,cartservice")      # sender: the real writer
    assert ",adservice," in dst.read_text().splitlines()[2]


def test_writer_ignores_an_edit_for_a_duplicated_id_written_by_another_service(tmp_path):
    src = tmp_path / "in.csv"
    src.write_text(HEADER + "18:35,t1,s1,frontendservice,GetCart,op,1000,1000000,10,0.0,p\n"
                            "18:35,t1,s1,cartservice,GetCart,op,1000,1000001,7,0.0,s1\n")
    dst = tmp_path / "out.csv"
    n = fb._write_traces(src, dst, {"s1": {"duration": 99, "_svc": "cartservice"}})
    out = dst.read_text().splitlines()
    assert n == 1 and ",10,0.0,p," in out[1] and ",99,0.0,s1," in out[2]


def test_log_edits_keep_the_row_order(tmp_path):
    logs = pd.DataFrame({"time": ["14:04", "14:04", "14:05"], "timestamp": [3, 1, 2],
                         "container_name": ["a", "b", "a"], "message": ["x", "y", "z"]})
    dst = tmp_path / "logs.csv"
    fb._write_logs(logs, dst, [], {0: {"timestamp": 9}})
    out = pd.read_csv(dst)
    assert out.timestamp.tolist() == [9, 1, 2] and out.sender.tolist() == ["a", "b", "a"]


def test_af1_sign_assignment_is_seeded_and_balanced():
    ids = [f"c{i}" for i in range(11)]
    a = honest.af1_sign_assignment(ids, 7)
    assert a == honest.af1_sign_assignment(list(reversed(ids)), 7)
    assert sorted(a) == sorted(ids) and abs(sum(v == "plus" for v in a.values()) - 5.5) <= 0.5


def _synthetic_case(root_cause: str) -> CleanCase:
    """frontend -> checkout -> {payment, email, currency}; frontend -> currency."""
    rows, n = [], 0
    for trace in range(40):
        f, c = f"f{trace}", f"c{trace}"
        rows += [(f, "frontendservice", "root"), (c, "checkoutservice", f)]
        for svc in ("paymentservice", "emailservice", "currencyservice"):
            rows.append((f"{svc[:2]}{trace}", svc, c))
        rows.append((f"cu{trace}", "currencyservice", f))
        n += 1
    t = pd.DataFrame(rows, columns=["spanID", "serviceName", "parentSpanID"])
    t["startTime"] = np.arange(len(t)) * 1_000_000 + 1_700_000_000_000_000
    t["duration"], t["statusCode"] = 10, 0
    psvc = t.parentSpanID.map(dict(zip(t.spanID, t.serviceName)))
    t["parent_service"] = psvc
    t["is_server"] = psvc.notna() & (psvc != t.serviceName)
    t["post"] = True
    case = CleanCase(case_id="syn", case_dir=Path("/nonexistent"), task={}, root_cause=root_cause,
                     fault="cpu", template="x")
    case.__dict__["spans"] = t
    return case


def test_af2_never_draws_the_root_cause_its_callers_or_the_trace_root():
    case = _synthetic_case("emailservice")
    res = honest.af2_edits(case, "currencyservice", np.random.default_rng(1))
    assert res.excluded == {"frontendservice": "trace_root", "emailservice": "root_cause",
                            "checkoutservice": "calls_root_cause"}
    assert res.b == "paymentservice" and res.capped_by_b
    # volume: min(k from D, share of B); only B-written spans are relabelled, to D's name
    assert res.requested == round(80 * honest.AF2_SHARE / (1 - honest.AF2_SHARE))
    assert res.requests == round(honest.AF2_SHARE * 40)
    sp = case.spans.set_index("spanID")
    assert all(sp.loc[s, "serviceName"] == "paymentservice" for s in res.span_edits)
    assert all(e == {"serviceName": "currencyservice"} for e in res.span_edits.values())


def test_af2_draw_is_seeded_over_every_eligible_neighbour():
    case = _synthetic_case("frontendservice")
    picks = {honest.af2_edits(case, "currencyservice", np.random.default_rng(s)).b for s in range(40)}
    assert picks == {"checkoutservice", "emailservice", "paymentservice"}


# --------------------------------------------------------------------------------------------
# Real-data layer
# --------------------------------------------------------------------------------------------

def _rows() -> list[dict]:
    idx = SEALED / "index.csv"
    return list(csv.DictReader(idx.open())) if idx.exists() else []


ROWS = _rows()
AF = [r for r in ROWS if r["family"] in fb.HONEST]
real_af = pytest.mark.skipif(not AF, reason="no AF variants in the development set")


@pytest.fixture(scope="module")
def evaluated():
    script = _load(REPO / "scripts" / "p3_honest_faults.py", "p3_honest_faults")
    from provrca_bench.framebench.case import load_dev_cases
    dev = {json.loads(l)["source"].split("/")[0] for l in ANSWERS.read_text().splitlines()}
    cases = {c.case_id: c for c in load_dev_cases(dataset_root=DATASET, tasks_root=TASKS,
                                                  answers_jsonl=ANSWERS, dev_templates=dev)}
    ids = sorted({r["case_id"] for r in AF})
    keep = ids if os.environ.get("PROVRCA_FULL") else ids[::4]
    jobs = [(cases[c], GEN, SEALED, sorted((r["variant"], r["family"], r["target"])
                                           for r in AF if r["case_id"] == c)) for c in keep]
    with ProcessPoolExecutor(WORKERS) as pool:
        return list(pool.map(script._one, jobs))


def _each(evaluated, family):
    for res in evaluated:
        for v, r in res["variants"].items():
            if r["family"] == family:
                yield f"{res['case_id']}/{v}", r


@real_af
def test_p3_t3_skew_changes_no_verdict_in_the_chain(evaluated):
    bad = {}
    for k, r in _each(evaluated, "AF1"):
        for mode in ("on", "off"):
            for kind in ("spans", "logs"):
                x = r[f"{kind}_attr_{mode}"]
                if x["changed_labelled"] or x["changed_genuine"]:
                    bad[f"{k}/{kind}/{mode}"] = x["genuine_changes"]
    assert bad == {}


@real_af
def test_p3_t4_mislabel_is_caught_by_attribution_not_by_the_rules(evaluated):
    bad = {}
    for k, r in _each(evaluated, "AF2"):
        on, off = r["spans_attr_on"], r["spans_attr_off"]
        if on["labelled_reasons"].get("ATTR_MISMATCH", 0) != on["labelled"]:
            bad[f"{k}/on/relabelled"] = on["labelled_reasons"]
        if off["labelled_verdicts"].get("contradicted", 0):
            bad[f"{k}/off/relabelled"] = off["labelled_verdicts"]
        for mode, x in (("on", on), ("off", off)):
            if x["genuine_contradicted"]:
                bad[f"{k}/{mode}/genuine"] = x["genuine_changes"]
    assert bad == {}


@pytest.mark.skipif(not ROWS, reason="no FrameBench development set")
def test_p3_t5_construction_over_every_adaptive_and_t1_variant():
    """P3-T5: F2c/F3c (and FSc, F2cD) give compatible or unknown; T1 forged callers are rejected by
    attribution. The checks are P2-T6's; this names the run and covers every family explicitly."""
    t6 = _load(REPO / "tests" / "test_p2_t6.py", "p2_t6")
    todo = [r for r in t6.ROWS if r["family"] in t6.ADAPTIVE or r["threat"] == "T1"]
    assert {r["family"] for r in todo} >= t6.ADAPTIVE
    assert {(r["family"], r["threat"]) for r in todo} >= {("F2", "T1"), ("F3", "T1")}
    with ProcessPoolExecutor(WORKERS) as pool:
        res = {f"{r['case_id']}/{v}": p for r, (v, p) in zip(todo, pool.map(t6._check, todo))}
    assert {k: v for k, v in res.items() if v} == {}
