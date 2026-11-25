"""P3 step 3: demotion at the tool layer -- M2 (withhold), M2-silent, M1 (annotate); D1 vs D4.

Constructed tests check the layer's logic; real-data tests (skipped without the dev data or the
harness interpreter) check the view against RCAEval's own toolkit on a FrameBench overlay.
"""
from __future__ import annotations

import gzip
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from provrca_bench.defence import AuditLog
from provrca_bench.provrca.demotion import (DEMOTES, MAX_NOTE_LINES, ProvRCALayer, build_view,
                                            case_records, drop_masks, split_records,
                                            write_case_audit, write_filtered)
from provrca_bench.provrca.rules import Config

sys.path.insert(0, str(Path(__file__).parent))
from test_framebench import IMPERATIVE  # noqa: E402  P2-T7's instruction patterns

CFG = Config.load()
TASK = {"standard_window": {"start": 0, "end": 100}, "inject_time": 50}


def rec(kind, service, t, verdict="unknown", reason="NO_WITNESS", detail="", operation="op",
        message="", pattern=""):
    return {"kind": kind, "service": service, "t": t, "verdict": verdict, "reason": reason,
            "detail": detail, "operation": operation, "message": message, "pattern": pattern,
            "record_id": f"{kind}:{service}:{t}"}


R1 = dict(verdict="contradicted", reason="R1_ENCLOSURE_FAIL")
R2 = dict(verdict="contradicted", reason="R2_STATUS_CONFLICT")
ATTR = dict(verdict="rejected", reason="ATTR_MISMATCH")


def records():
    return pd.DataFrame([
        rec("span", "currencyservice", 60, **R1), rec("span", "currencyservice", 61, **R1),
        rec("span", "currencyservice", 70, verdict="compatible", reason="OK_ENCLOSED"),
        rec("span", "paymentservice", 10, **R2),
        rec("span", "emailservice", 150, **R1),                       # outside the window
        rec("span", "frontendservice", 55, **ATTR),
        rec("log", "frontend", 60, message="GetQuote failed: rpc error", pattern="p1", **ATTR),
        rec("log", "frontend", 60, message="request started", pattern="p2", detail="no_log_rule"),
    ])


def layer(mode="M2", arm="D4"):
    return ProvRCALayer(arm=arm, mode=mode, records=records(), task=TASK)


# --------------------------------------------------------------- M2 notes
def test_m2_note_is_scoped_to_the_call():
    out, v = layer().filter("trace_summary", {"service": "currencyservice"}, {"rows": []}, None)
    assert out["withheld"] == ["2 spans from currencyservice withheld: duration exceeds the caller's wait (R1)"]
    assert v == []
    out, _ = layer().filter("trace_summary", {"start": 0, "end": 50}, {"rows": []}, None)
    assert out["withheld"] == ["1 spans from paymentservice withheld: status code conflicts with "
                               "the code the caller received (R2)"]


def test_m2_search_logs_note_follows_service_and_pattern():
    out, _ = layer().filter("search_logs", {"pattern": "failed"}, {"groups": []}, None)
    assert out["withheld"] == ["1 log lines from frontend withheld: sender does not match the claimed service"]
    raw = {"groups": []}
    assert layer().filter("search_logs", {"pattern": "started"}, raw, None)[0] is raw


def test_m2_case_wide_tools_list_everything_withheld_in_the_window():
    out, _ = layer().filter("rank_anomalies", {"modality": "trace_latency"}, {"ranked": []}, None)
    assert len(out["withheld"]) == 3          # currency R1, payment R2, frontend ATTR; email is outside
    out, _ = layer().filter("call_graph", {}, {"edges": []}, None)
    assert len(out["withheld"]) == 3


@pytest.mark.parametrize("tool,args", [("get_metric", {"name": "x"}), ("list_metrics", {}),
                                       ("describe_case", {}), ("rank_anomalies", {"modality": "metrics"}),
                                       ("submit", {"ranks": ["a"]})])
def test_tools_that_read_no_span_or_log_are_untouched(tool, args):
    raw = {"anything": 1}
    for mode in ("M2", "M1"):
        assert layer(mode).filter(tool, args, raw, None)[0] is raw


def test_no_key_when_nothing_is_withheld_and_errors_pass_through():
    raw = {"rows": []}
    assert layer().filter("trace_summary", {"service": "emailservice"}, raw, None)[0] is raw
    err = {"error": "bad"}
    assert layer().filter("trace_summary", {}, err, None)[0] is err


def test_m2_silent_returns_the_output_unchanged():
    raw = {"rows": []}
    assert layer("M2-silent").filter("trace_summary", {}, raw, None)[0] is raw


def test_note_is_bounded():
    many = pd.DataFrame([rec("span", f"svc{i}", 60, **R1) for i in range(40)])
    out, _ = ProvRCALayer(arm="D4", mode="M2", records=many, task=TASK).filter(
        "call_graph", {}, {"edges": []}, None)
    assert len(out["withheld"]) == MAX_NOTE_LINES
    assert out["withheld"][-1] == f"{40 - MAX_NOTE_LINES + 1} further records withheld"


def test_notes_carry_no_record_content_or_instructions():
    out, _ = layer().filter("search_logs", {}, {"groups": []}, None)
    text = " ".join(out["withheld"])
    assert "GetQuote" not in text and not IMPERATIVE.search(text)


# --------------------------------------------------------------- D1 vs D4
def test_d1_withholds_only_rejected_records():
    out, _ = layer(arm="D1").filter("call_graph", {}, {"edges": []}, None)
    assert out["withheld"] == ["1 spans from frontendservice withheld: sender does not match the claimed service"]
    assert DEMOTES["D1"] == {"rejected"} and DEMOTES["D4"] == {"rejected", "contradicted"}


# --------------------------------------------------------------- M1
def test_m1_tags_each_row_and_keeps_the_output():
    raw = {"rows": [{"service": "currencyservice", "operation": "op", "after": {"requests": 3}},
                    {"service": "nobody", "operation": "op"}]}
    out, _ = layer("M1").filter("trace_summary", {}, raw, None)
    assert out["rows"][0]["verdicts"] == {
        "contradicted: duration exceeds the caller's wait (R1)": 2,
        "compatible: a rule applied and held (not verified)": 1}
    assert "verdicts" not in out["rows"][1]
    assert {k: v for k, v in out["rows"][0].items() if k != "verdicts"} == raw["rows"][0]
    out, _ = layer("M1").filter("search_logs", {}, {"groups": [
        {"service": "frontend", "pattern": "p2"}]}, None)
    assert out["groups"][0]["verdicts"] == {"unknown: NO_WITNESS no_log_rule": 1}


# --------------------------------------------------------------- records of a file
def test_split_records_handles_quoted_newlines_and_quotes(tmp_path):
    p = tmp_path / "logs.csv"
    body = 'a,b\n1,"x\ny"\n2,"he said ""hi"""\n3,z\n'
    p.write_text(body, newline="")
    header, recs = split_records(p)
    assert header == "a,b\n" and recs == ['1,"x\ny"\n', '2,"he said ""hi"""\n', "3,z\n"]
    assert len(recs) == len(pd.read_csv(p))
    write_filtered(p, tmp_path / "out.csv", np.array([False, True, False]))
    assert (tmp_path / "out.csv").read_text() == 'a,b\n1,"x\ny"\n3,z\n'
    with pytest.raises(ValueError):
        write_filtered(p, tmp_path / "x.csv", np.array([True]))


def test_empty_drop_set_returns_the_input_view(tmp_path):
    assert build_view(tmp_path, tmp_path / "v", drop_spans=np.zeros(3, bool),
                      drop_logs=np.zeros(2, bool), python="unused") == tmp_path


# --------------------------------------------------------------- audit
def test_case_audit_has_one_line_per_withheld_item(tmp_path):
    r = records().assign(route="", tier="", r1_reason="", r2_reason="", server_us="12",
                         client_us="10", server_code="0", client_code="0")
    meta = write_case_audit(tmp_path, run_id="run1", case_id="c1", records=r, arm="D4", cfg=CFG,
                            view=tmp_path)
    log = AuditLog(tmp_path / "audit.jsonl")
    assert len(log.read()) == 6 == meta["withheld"]          # 4 contradicted + 2 rejected in total
    assert len(log.withheld(DEMOTES["D1"])) == 2
    span = next(x for x in log.read() if x.record_id.startswith("span:currencyservice"))
    assert span.rule_inputs["server_us"] == "12" and span.rule_inputs["table_sha256"] == CFG.sha256
    assert span.rule_inputs["epsilon_us"] == 0
    with gzip.open(tmp_path / "verdicts-c1.csv.gz", "rt") as f:
        assert len(pd.read_csv(f)) == len(r)


# --------------------------------------------------------------- real data
REPO = Path(__file__).resolve().parents[1]
GEN = Path(os.environ.get("PROVRCA_FRAMEBENCH_ROOT", REPO / "cases" / "framebench-dev"))
TASKS = REPO / "parity" / "tasks-rebuilt" / "re2-ob" / "cases"
PY = os.environ.get("PROVRCA_HARNESS_PYTHON", str(Path.home() / "g0_pilot/.venv/bin/python"))
HAVE = (GEN / "re2-ob-0001" / "F2-T2-high-20261125").exists() and TASKS.exists() and Path(PY).exists()
real = pytest.mark.skipif(not HAVE, reason="needs the FrameBench dev set, the task views and the harness interpreter")


@pytest.fixture(scope="module")
def overlay(tmp_path_factory):
    from RCAEval.agent.toolkit import CaseToolkit

    from provrca_bench.taskview import make_task_view
    root = tmp_path_factory.mktemp("views")
    out = {}
    for var in ("clean", "F2-T2-high-20261125", "F3-T2-high-20261125"):
        tv = make_task_view(GEN / "re2-ob-0001" / var, TASKS / "re2-ob-0001" / "task.json",
                            root / "tv" / var.replace("-", "")[:8])
        r = case_records(tv, CFG)
        ds, dl = drop_masks(r, DEMOTES["D4"])
        view = build_view(tv, root / "v", drop_spans=ds, drop_logs=dl, python=PY,
                          force=(var == "clean"))
        out[var] = dict(tv=tv, view=view, records=r, ds=ds, dl=dl,
                        raw=CaseToolkit(str(tv)), dem=CaseToolkit(str(view)))
    return out


@real
def test_view_of_a_clean_case_reproduces_every_derived_file(overlay):
    o = overlay["clean"]
    assert not o["ds"].any() and not o["dl"].any()           # P3-T2: nothing withheld on clean
    for f in sorted(p.name for p in o["tv"].iterdir()):
        assert (o["view"] / f).read_bytes() == (o["tv"] / f).read_bytes(), f


@real
@pytest.mark.parametrize("var", ["F2-T2-high-20261125", "F3-T2-high-20261125"])
def test_view_drops_exactly_the_withheld_records(overlay, var):
    o = overlay[var]
    assert o["ds"].sum() > 0
    for f in ("task.json", "metrics.csv", "metrics_raw.csv"):
        assert (o["view"] / f).read_bytes() == (o["tv"] / f).read_bytes()
    _, before = split_records(o["tv"] / "traces.csv")
    _, after = split_records(o["view"] / "traces.csv")
    assert after == [r for r, d in zip(before, o["ds"]) if not d]


@real
@pytest.mark.parametrize("var", ["F2-T2-high-20261125", "F3-T2-high-20261125"])
def test_m2_recomputes_and_says_what_it_withheld(overlay, var):
    o = overlay[var]
    task = json.loads((o["tv"] / "task.json").read_text())
    target = o["records"][o["records"].verdict == "contradicted"].service.iloc[0]
    args = {"service": target, "top_k": 100}
    L = ProvRCALayer(arm="D4", mode="M2", records=o["records"], task=task)
    raw, dem = o["raw"].call("trace_summary", args), o["dem"].call("trace_summary", args)
    out, _ = L.filter("trace_summary", args, dem, None)
    n = lambda res: sum((r["before"] or {}).get("requests", 0) + (r["after"] or {}).get("requests", 0)  # noqa: E731
                        for r in res["rows"])
    withheld = int(out["withheld"][0].split()[0])
    assert n(raw) - n(dem) == withheld == int(o["ds"].sum())
    assert o["dem"].call("get_metric", {"name": f"{target}_cpu"}) == \
        o["raw"].call("get_metric", {"name": f"{target}_cpu"})
    m1, _ = ProvRCALayer(arm="D4", mode="M1", records=o["records"], task=task).filter(
        "trace_summary", args, raw, None)
    assert [{k: v for k, v in r.items() if k != "verdicts"} for r in m1["rows"]] == raw["rows"]


@real
def test_d1_equals_d0_under_t2(overlay):
    """P4-T3: under T2 the target sends its own forgeries, so D1 withholds nothing and its view is
    the task view itself."""
    for var in ("F2-T2-high-20261125", "F3-T2-high-20261125"):
        ds, dl = drop_masks(overlay[var]["records"], DEMOTES["D1"])
        assert not ds.any() and not dl.any()
        assert build_view(overlay[var]["tv"], Path("/nonexistent"), drop_spans=ds, drop_logs=dl,
                          python=PY) == overlay[var]["tv"]


@real
def test_split_records_matches_pandas_on_every_clean_log_file():
    for d in sorted(TASKS.iterdir()):
        _, recs = split_records(d / "logs.csv")
        assert len(recs) == len(pd.read_csv(d / "logs.csv", usecols=[0])), d.name
