"""The K22 revision (27 Nov): T1 as replayed requests placed at genuine request times, and log
insertions that replay a sibling case's episodes (A1 whole; F1/F4 as runs whose episodes end in
the forged line). Unit tests run anywhere; the real-data
tests need the rebuilt development cases and skip without them."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from provrca_bench.framebench import adaptive, build as fb
from provrca_bench.framebench.case import load_dev_cases, log_name
from provrca_bench.framebench.doses import dose_for
from provrca_bench.framebench.target import seed_for

REPO = Path(__file__).resolve().parents[1]
DATASET = REPO / "parity" / "dataset-rebuilt"
TASKS = REPO / "parity" / "tasks-rebuilt" / "re2-ob" / "cases"
ANSWERS = REPO / "parity" / "tasks-rebuilt" / "re2-ob.answers.jsonl"
TARGETS = REPO / "sealed" / "framebench-dev" / "targets.csv"
SEED = 20261125
real = pytest.mark.skipif(not (ANSWERS.exists() and TARGETS.exists()),
                          reason="no rebuilt development cases")


# ------------------------------------------------------------------ units
def test_runs_group_by_key_and_gap():
    ts = np.array([0, 5, 2_000, 2_003, 10_000, 1])
    key = np.array(["a", "a", "a", "a", "a", "b"])
    runs = fb._runs(ts, key, 10)
    assert sorted(map(sorted, map(list, runs))) == [[0, 1], [2, 3], [4], [5]]
    eps = fb._runs(ts, None, 10)                    # without a key, b's line joins a's first run
    assert sorted(map(sorted, map(list, eps))) == [[0, 1, 5], [2, 3], [4]]


def test_hhmm_is_utc():
    assert fb._hhmm(1705696521129621311) == "20:35"
    assert adaptive._hhmm_us(1705696521129626) == "20:35"


# ------------------------------------------------------------------ real data
@pytest.fixture(scope="module")
def case_and_target():
    dev = {json.loads(l)["source"].split("/")[0] for l in ANSWERS.read_text().splitlines()}
    cases = load_dev_cases(dataset_root=DATASET, tasks_root=TASKS, answers_jsonl=ANSWERS,
                           dev_templates=dev)
    tg = {r["case_id"]: r["service"] for r in csv.DictReader(TARGETS.open()) if r["drawn"] == "True"}
    c = next(c for c in cases if tg[c.case_id] == "paymentservice")    # payment: paired lines
    return c, tg[c.case_id]


@real
@pytest.mark.parametrize("family", ["F1", "F4", "A1"])
def test_log_inserts_keep_count_window_and_never_sit_next_to_their_copy(case_and_target, family):
    case, target = case_and_target
    logs = pd.read_csv(case.case_dir / "logs.csv", low_memory=False, keep_default_na=False)
    dose = dose_for(case, target, "high")
    v = fb.variant_name(family, "T2", "high", SEED)
    rows = fb._log_inserts(case, logs, target, family, dose,
                           np.random.default_rng(seed_for(case.case_id, v, target)))
    forged = [r for r in rows if not r.get("_witness")]
    assert len(forged) == dose.f1_lines                     # the dose counts forged lines only
    if family == "A1":
        assert len(rows) == dose.f1_lines
    lo, hi = ((case.inject_time - 300, case.inject_time - 120) if family == "F4"
              else (case.inject_time, case.window[1]))
    mine = logs[logs.container_name == log_name(target)]
    gts = mine.timestamp.astype("int64").to_numpy()
    gmsg = mine.message.astype(str).to_numpy()
    for r in rows:
        assert lo <= r["timestamp"] // 10**9 < hi
        assert r["time"] == fb._hhmm(r["timestamp"])
        assert r["container_name"] == log_name(target)
        close = np.abs(gts - r["timestamp"]) <= 1_000_000
        assert not (close & (gmsg == r["message"])).any(), "a copy next to its original (K22 v1)"
    donor = set(fb.a1_donor_lines(case, log_name(target), pre=family == "F4").message.astype(str))
    if family == "A1":                       # content from the sibling repetition, not this file
        own = set(gmsg)
        assert {r["message"] for r in rows} <= donor
        assert any(r["message"] not in own for r in rows)
    else:                                    # round 2: companions are donor lines; every forged
        comp = [r for r in rows if r.get("_witness")]   # line ends its episode
        assert {r["message"] for r in comp} <= donor
        assert all(r["level"].lower() in ("error", "warning") for r in forged)
        ts = sorted(r["timestamp"] for r in rows)
        for r in comp:                       # a companion is followed within 10 ms by its episode
            nxt = [t for t in ts if t > r["timestamp"]]
            assert nxt and nxt[0] - r["timestamp"] <= fb.EPISODE_GAP_NS


@real
@pytest.mark.parametrize("family", ["F2", "F3"])
def test_t1_replays_whole_traces_well_nested_and_in_window(case_and_target, family):
    case, target = case_and_target
    dose = dose_for(case, target, "low")
    v = fb.variant_name(family, "T1", "low", SEED)
    res = adaptive.t1_inserts(case, target, family, dose,
                              np.random.default_rng(seed_for(case.case_id, v, target)))
    assert res.server_ids and res.replays
    tr = adaptive._clean_frame(str(case.case_dir))
    f = pd.read_csv(io.StringIO("".join(l for ls in res.lines.values() for l in ls)),
                    names=list(tr.columns) + ["sender"], dtype=str, keep_default_na=False)
    assert (f.sender == adaptive.ATTACKER).all()
    assert set(f.spanID) == set(res.server_ids) | set(res.client_ids) | set(res.witness_ids)
    assert not set(f.spanID) & set(tr.spanID) and not set(f.traceID) & set(tr.traceID)
    st = f.startTime.astype("int64")
    assert (st >= case.inject_time * 10**6).all() and (st < case.window[1] * 10**6).all()
    assert (f.time == st.map(adaptive._hhmm_us)).all()
    assert (f.startTimeMillis.astype("int64") == st // 1000).all()
    # every replayed trace has the service/method multiset of some genuine trace
    key = lambda d: tuple(sorted(zip(d.serviceName, d.methodName)))     # noqa: E731
    genuine = set(tr[tr.traceID.isin(set(tr.traceID.sample(min(20000, tr.traceID.nunique()),
                                                              random_state=0)) |
                                       set(tr.traceID[tr.serviceName == target]))]
                  .groupby("traceID").apply(key))
    assert f.groupby("traceID").apply(key).isin(genuine).mean() == 1.0
    # each claim is enclosed by its ancestors inside the copy (genuine traces carry a little clock
    # skew, so this is a rate, not a law: the F3 lengthening and the dilation must not break it)
    by = f.set_index("spanID")
    end = st.values + f.duration.astype("int64").values
    s_of, e_of = dict(zip(f.spanID, st.values)), dict(zip(f.spanID, end))
    ok = []
    for c in res.server_ids:
        p = by.at[c, "parentSpanID"]
        while p in s_of:
            ok.append(s_of[p] <= s_of[c] and e_of[c] <= e_of[p])
            p = by.at[p, "parentSpanID"]
    assert ok and np.mean(ok) >= 0.99, (family, np.mean(ok))
    if family == "F2":
        assert (by.loc[res.server_ids, "statusCode"] == f"{float(dose.f2_code)}").all()
    # round 2: density-matched placement: each copy's first claim starts within 2 s of a genuine
    # target server start
    g = np.sort(case.server_spans(target).startTime.astype("int64").to_numpy())
    firsts = f[f.spanID.isin(set(res.server_ids))].groupby("traceID").startTime.apply(
        lambda x: x.astype("int64").min()).to_numpy()
    i = np.clip(np.searchsorted(g, firsts), 1, len(g) - 1)
    near = np.minimum(np.abs(g[i] - firsts), np.abs(g[i - 1] - firsts))
    assert (near <= adaptive.T1_JITTER_US).all()


@real
@pytest.mark.parametrize("case_id,family", [("re2-ob-0028", "F4"), ("re2-ob-0028", "F1")])
def test_log_events_never_merge_with_each_other(case_id, family):
    """Fix, 28 Nov: two F4-high events of different messages coincided in re2-ob-0028 (one
    timestamp, two lines), so the label contract failed. Every inserted episode (lines <= 10 ms
    apart) holds exactly one forged line, which ends it."""
    dev = {json.loads(l)["source"].split("/")[0] for l in ANSWERS.read_text().splitlines()}
    case = next(c for c in load_dev_cases(dataset_root=DATASET, tasks_root=TASKS,
                                          answers_jsonl=ANSWERS, dev_templates=dev)
                if c.case_id == case_id)
    target = {r["case_id"]: r["service"] for r in csv.DictReader(TARGETS.open())
              if r["drawn"] == "True"}[case_id]
    logs = pd.read_csv(case.case_dir / "logs.csv", low_memory=False, keep_default_na=False)
    dose = dose_for(case, target, "high")
    v = fb.variant_name(family, "T2", "high", SEED)
    rows = fb._log_inserts(case, logs, target, family, dose,
                           np.random.default_rng(seed_for(case.case_id, v, target)))
    rows = sorted(rows, key=lambda r: r["timestamp"])
    ts = np.array([r["timestamp"] for r in rows], dtype=np.int64)
    assert len(set(ts.tolist())) == len(ts)
    grp = np.r_[0, np.cumsum(np.diff(ts) > fb.EPISODE_GAP_NS)]
    for g in np.unique(grp):
        ep = [r for r, k in zip(rows, grp) if k == g]
        assert sum(not r["_witness"] for r in ep) == 1 and not ep[-1]["_witness"]
