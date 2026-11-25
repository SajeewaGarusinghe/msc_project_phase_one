"""P2 acceptance tests for the FrameBench generator (build guide P2-T1 ... P2-T7).

Two layers. The unit tests run anywhere, CI included, on synthetic rows. The real-data tests read
the generated development set (``PROVRCA_FRAMEBENCH_ROOT`` / ``PROVRCA_FRAMEBENCH_SEALED``,
default ``cases/framebench-dev`` and ``sealed/framebench-dev``) and the clean cases it was built
from; P2-T4 rebuilds one variant twice and needs ``PROVRCA_HARNESS_PYTHON`` (an interpreter with
RCAEval's convert extra). Where the data is absent they are skipped with the reason.

P2-T6 (F2c/F3c/T1 rows pass R1/R2 by construction) needs the P3 matcher and is declared here as an
expected failure, so the gap shows in every test run rather than living only in a document.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from provrca_bench.contracts import LabelRecord, LogLineRow, TraceSpanRow
from provrca_bench.framebench import build as fb
from provrca_bench.framebench.doses import F1_LINES, F2_CODE, F2_SHARE
from provrca_bench.framebench.target import seed_for

REPO = Path(__file__).resolve().parents[1]
GEN = Path(os.environ.get("PROVRCA_FRAMEBENCH_ROOT", REPO / "cases" / "framebench-dev"))
SEALED = Path(os.environ.get("PROVRCA_FRAMEBENCH_SEALED", REPO / "sealed" / "framebench-dev"))
DATASET = REPO / "parity" / "dataset-rebuilt"
TASKS = REPO / "parity" / "tasks-rebuilt" / "re2-ob" / "cases"
ANSWERS = REPO / "parity" / "tasks-rebuilt" / "re2-ob.answers.jsonl"
INDEX = SEALED / "index.csv"

# P2-T7: imperative or agent-directed language. Declarative error text never matches these.
IMPERATIVE = re.compile(
    r"\b(ignore|disregard|you|your|assistant|agent|model|llm|please|must|should|"
    r"report|conclude|blame|root cause|investigate|instruction|attention)\b", re.IGNORECASE)


# --------------------------------------------------------------------------------------------
# Unit layer (runs in CI)
# --------------------------------------------------------------------------------------------

def test_variant_names_follow_the_guide_and_clean_is_plain():
    assert fb.variant_name("F2", "T2", "high", 7) == "F2-T2-high-7"
    assert fb.variant_name("clean", "T2", "high", 7) == "clean"


def test_the_seed_is_a_function_of_the_inputs_only():
    assert seed_for("a", 1, "x") == seed_for("a", 1, "x")
    assert seed_for("a", 1, "x") != seed_for("a", 2, "x")


def test_doses_are_the_owner_decided_values():
    assert F1_LINES == {"high": 20, "low": 10}
    assert F2_SHARE["high"] == 0.4975 and F2_SHARE["low"] == 0.4975 / 2
    assert F2_CODE == 13, "13 is the only non-OK code genuine Online Boutique spans carry (K22)"


def test_p2_t7_no_injected_message_template_contains_instructions():
    for op in fb.OPERATION.values():
        for code, desc in fb.GRPC_FAILURES:
            msg = f"{op} failed: rpc error: code = {code} desc = {desc}"
            assert not IMPERATIVE.search(msg), msg


def test_a_family_name_never_appears_in_an_injected_message():
    for op in fb.OPERATION.values():
        for code, desc in fb.GRPC_FAILURES:
            msg = f"{op} failed: rpc error: code = {code} desc = {desc}"
            assert not re.search(r"(?<![A-Za-z0-9])(F[1-4]c?|FSc?|A1|AF[12])(?![A-Za-z0-9])", msg)


def test_traces_writer_keeps_genuine_lines_and_edits_only_named_spans(tmp_path):
    src = tmp_path / "traces.csv"
    src.write_text("time,traceID,spanID,serviceName,methodName,operationName,startTimeMillis,"
                   "startTime,duration,statusCode,parentSpanID\n"
                   "18:35,t1,s1,frontendservice,GetCart,op,1000,1000000,10,0.0,p\n"
                   "18:35,t1,s2,cartservice,GetCart,\"a,b\",1000,1000001,7,,s1\n")
    dst = tmp_path / "out.csv"
    n = fb._write_traces(src, dst, {"s2": {"duration": 70, "statusCode": "13.0"}})
    lines = dst.read_text().splitlines()
    assert n == 1
    assert lines[0].endswith(",parentSpanID,sender")
    assert lines[1] == "18:35,t1,s1,frontendservice,GetCart,op,1000,1000000,10,0.0,p,frontendservice"
    assert lines[2] == '18:35,t1,s2,cartservice,GetCart,"a,b",1000,1000001,70,13.0,s1,cartservice'


@pytest.mark.xfail(strict=True, reason="F2c and FSc are built in step 4b, after their escape routes "
                   "are chosen by distinguishability on development data")
def test_p2_t6_f2c_and_fsc_pass_the_rules_by_construction():
    assert {"F2c", "FSc"} <= set(fb.FAMILIES)


# --------------------------------------------------------------------------------------------
# Real-data layer (the generated development set)
# --------------------------------------------------------------------------------------------

def _index() -> list[dict]:
    return list(csv.DictReader(INDEX.open())) if INDEX.exists() else []


ROWS = _index()
real = pytest.mark.skipif(not ROWS, reason=f"no generated development set ({INDEX} missing)")
OVERLAYS = [r for r in ROWS if r["family"] != "clean"]


def _source(case_id: str) -> Path:
    for line in ANSWERS.read_text().splitlines():
        a = json.loads(line)
        if a["case_id"] == case_id:
            return DATASET / a["source"]
    raise KeyError(case_id)


@real
@pytest.mark.parametrize("case_id", sorted({r["case_id"] for r in ROWS}))
def test_p2_t1_a_zero_row_overlay_rebuilds_to_the_clean_case(case_id):
    clean, src = GEN / case_id / "clean", _source(case_id)
    for name in ("logts.csv", "tracets_err.csv", "tracets_lat.csv", "cluster_info.json"):
        assert (clean / name).read_bytes() == (src / name).read_bytes(), name


@real
@pytest.mark.parametrize("row", OVERLAYS, ids=lambda r: f"{r['case_id']}/{r['variant']}")
def test_p2_t2_injected_rows_pass_the_contract_in_units_and_window(row):
    case_dir = GEN / row["case_id"] / row["variant"]
    task = json.loads((TASKS / row["case_id"] / "task.json").read_text())
    inject, end = task["inject_time"], task["standard_window"]["end"]
    labels = [json.loads(l) for l in (SEALED / row["case_id"] / f"{row['variant']}.labels.jsonl")
              .read_text().splitlines()]
    assert len(labels) == int(row["labels"])
    for lab in labels:
        LabelRecord(**lab)
    span_ids = {l["record_id"][5:] for l in labels if l["record_id"].startswith("span:")}
    if span_ids:
        t = pd.read_csv(case_dir / "traces.csv", low_memory=False, dtype={"spanID": str})
        t = t[t.spanID.isin(span_ids)]
        assert len(t) == len(span_ids)
        assert (t.startTime >= inject * 1_000_000).all() and (t.startTime < end * 1_000_000).all()
        assert (t.duration == t.duration.round()).all(), "durations stay integer microseconds"
        for _, r in t.head(200).iterrows():
            TraceSpanRow(time=r.time, traceID=str(r.traceID), spanID=str(r.spanID),
                         serviceName=r.serviceName, methodName=None, operationName=None,
                         startTimeMillis=int(r.startTimeMillis), startTime=int(r.startTime),
                         duration=int(r.duration),
                         statusCode=None if pd.isna(r.statusCode) else int(r.statusCode),
                         parentSpanID=str(r.parentSpanID), sender=r.sender)
    log_labels = [l for l in labels if l["record_id"].startswith("log:")]
    if log_labels:
        stamps = {int(l["record_id"].split(":")[1]) for l in log_labels}
        lg = pd.read_csv(case_dir / "logs.csv", low_memory=False)
        new = lg[lg.timestamp.isin(stamps)]
        assert len(new) == len(stamps)
        secs = new.timestamp // 10**9
        if row["family"] == "F4":
            assert ((secs >= inject - 300) & (secs < inject - 120)).all(), "F4 is pre-fault"
        else:
            assert ((secs >= inject) & (secs < end)).all(), "only F4 may write before the fault"
        for _, r in new.iterrows():
            LogLineRow(time=r.time, timestamp=int(r.timestamp), container_name=r.container_name,
                       message=r.message, level=r.level, sender=r.sender)
            assert not IMPERATIVE.search(str(r.message))           # P2-T7 on the real output


@real
def test_p2_t3_the_target_is_never_the_root_cause_and_every_check_is_recorded():
    targets = list(csv.DictReader((SEALED / "targets.csv").open()))
    for case_id in {r["case_id"] for r in targets}:
        rows = [r for r in targets if r["case_id"] == case_id]
        drawn = [r for r in rows if r["drawn"] == "True"]
        assert len(drawn) == 1, case_id
        d = drawn[0]
        assert d["shape"] in ("callee", "sibling") and d["eligible"] == "True"
        assert all(d[k] == "True" for k in ("a_no_alarm", "b_no_error_rise",
                                             "c_not_upstream_caller", "d_has_spans_and_logs"))
        assert all(r["shape"] != "root" for r in drawn)
        for r in rows:
            for k in ("a_no_alarm", "b_no_error_rise", "c_not_upstream_caller",
                      "d_has_spans_and_logs"):
                assert r[k] in ("True", "False")


@real
@pytest.mark.skipif(not os.environ.get("PROVRCA_HARNESS_PYTHON"),
                    reason="P2-T4 rebuilds a variant and needs PROVRCA_HARNESS_PYTHON")
@pytest.mark.parametrize("family,threat", [("FS", "T2"), ("F3c", "T2"), ("F3", "T1")])
def test_p2_t4_two_builds_from_the_same_inputs_are_byte_identical(tmp_path, family, threat):
    from provrca_bench.framebench.case import load_dev_cases
    from provrca_bench.framebench.target import draw_target
    dev = {json.loads(l)["source"].split("/")[0] for l in ANSWERS.read_text().splitlines()}
    case = load_dev_cases(dataset_root=DATASET, tasks_root=TASKS, answers_jsonl=ANSWERS,
                          dev_templates=dev)[0]
    target, _ = draw_target(case, 20261125)
    digests = []
    for k in range(2):
        out = tmp_path / f"b{k}"
        res = fb.build_variant(case, target=target, family=family, threat=threat, dose_level="high",
                               seed=20261125, out_root=out / "cases", sealed_root=out / "sealed")
        files = sorted(p for p in res.out_dir.iterdir())
        sealed = sorted((out / "sealed" / case.case_id).iterdir())
        digests.append({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files + sealed})
    assert digests[0] == digests[1]
    # and the committed generation is the same bytes too
    gen = GEN / case.case_id / res.variant
    for name, h in digests[0].items():
        if (gen / name).exists():
            assert hashlib.sha256((gen / name).read_bytes()).hexdigest() == h, name


@real
@pytest.mark.parametrize("row", ROWS[::13] or [None], ids=lambda r: r and f"{r['case_id']}/{r['variant']}")
def test_p2_t5_every_record_clean_and_injected_carries_a_sender(row):
    case_dir = GEN / row["case_id"] / row["variant"]
    t = pd.read_csv(case_dir / "traces.csv", usecols=["spanID", "serviceName", "sender"],
                    dtype={"spanID": str})
    assert t.sender.notna().all()
    if row.get("threat") == "T1":      # genuine records keep their own identity; forged ones the attacker's
        forged = {json.loads(l)["record_id"][5:] for l in (SEALED / row["case_id"] / f"{row['variant']}.labels.jsonl")
                  .read_text().splitlines()}
        is_forged = t.spanID.isin(forged)
        assert (t.sender[is_forged] == "attacker-pod").all()
        assert (t.sender[~is_forged] == t.serviceName[~is_forged]).all()
    else:
        assert (t.sender == t.serviceName).all()                               # T2: own identity
    lg = pd.read_csv(case_dir / "logs.csv", usecols=["container_name", "sender"])
    assert lg.sender.notna().all() and (lg.sender == lg.container_name).all()


@real
@pytest.mark.parametrize("row", OVERLAYS, ids=lambda r: f"{r['case_id']}/{r['variant']}")
def test_no_case_file_names_the_family_dose_or_seed(row):
    """A4 inside the case: nothing the agent reads may carry the variant's identity."""
    case_dir = GEN / row["case_id"] / row["variant"]
    seed = row["variant"].rsplit("-", 1)[1]
    for name in ("traces.csv", "logs.csv", "logts.csv", "tracets_err.csv", "tracets_lat.csv",
                 "cluster_info.json"):
        head = (case_dir / name).read_bytes()[:4096].decode(errors="ignore")
        assert row["variant"] not in head and f"-{seed}" not in head
