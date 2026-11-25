"""Build-guide P1 item 2: "A schema test reads every case file through them".

The P1 contracts were checked only against hand-written rows and two fields turned out wrong on real
data (``time`` and the log ``timestamp``). This test reads every clean development case: whole
columns are checked vectorised (service names, start fields, units), and a seeded sample of rows
from each file is validated through the pydantic models. Clean files carry no ``sender`` column, so
the genuine writer is filled in, as FrameBench does for every clean record.

The data lives outside the repository. Set ``PROVRCA_DEV_ROOT`` to the rebuilt development tree
(default ``parity/dataset-rebuilt``); without it the test is skipped with the reason.
"""
import os
from pathlib import Path

import pandas as pd
import pytest

from provrca_bench.contracts import LogLineRow, TraceSpanRow

ROOT = Path(os.environ.get("PROVRCA_DEV_ROOT",
                           Path(__file__).resolve().parents[1] / "parity" / "dataset-rebuilt"))
CASES = sorted(p for p in ROOT.glob("*/*") if (p / "traces.csv").exists()) if ROOT.exists() else []
SAMPLE = 500

pytestmark = pytest.mark.skipif(not CASES, reason=f"no development cases under {ROOT}")


def _none(v):
    return None if pd.isna(v) else v


@pytest.mark.parametrize("case", CASES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_trace_file_fits_the_contract(case):
    t = pd.read_csv(case / "traces.csv", low_memory=False)
    assert list(t.columns) == ["time", "traceID", "spanID", "serviceName", "methodName",
                               "operationName", "startTimeMillis", "startTime", "duration",
                               "statusCode", "parentSpanID"]
    assert not t.serviceName.astype(str).str.contains("_").any()
    assert ((t.startTime // 1000 - t.startTimeMillis).abs() <= 1).all()
    assert (t.duration >= 0).all()
    codes = t.statusCode.dropna()
    assert (codes == codes.round()).all(), "status codes are whole numbers written as floats"
    for _, r in t.sample(n=min(SAMPLE, len(t)), random_state=20261125).iterrows():
        TraceSpanRow(time=r.time, traceID=str(r.traceID), spanID=str(r.spanID),
                     serviceName=r.serviceName, methodName=_none(r.methodName),
                     operationName=_none(r.operationName), startTimeMillis=int(r.startTimeMillis),
                     startTime=int(r.startTime), duration=int(r.duration),
                     statusCode=None if pd.isna(r.statusCode) else int(r.statusCode),
                     parentSpanID=_none(r.parentSpanID) and str(r.parentSpanID),
                     sender=r.serviceName)


@pytest.mark.parametrize("case", CASES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_log_file_fits_the_contract(case):
    lg = pd.read_csv(case / "logs.csv", low_memory=False)
    assert list(lg.columns)[:7] == ["time", "timestamp", "container_name", "message", "level",
                                    "req_path", "error"]
    assert not lg.container_name.astype(str).str.contains("_").any()
    inject = int((case / "inject_time.txt").read_text())
    # nanoseconds: the file spans the fault time when read at 1e9
    assert lg.timestamp.min() // 10**9 < inject < lg.timestamp.max() // 10**9
    for _, r in lg.sample(n=min(SAMPLE, len(lg)), random_state=20261125).iterrows():
        LogLineRow(time=r.time, timestamp=int(r.timestamp), container_name=r.container_name,
                   message=str(r.message), level=_none(r.level),
                   req_path=_none(r.req_path) and str(r.req_path),
                   error=_none(r.error) and str(r.error),
                   cluster_id=None if pd.isna(r.cluster_id) else int(r.cluster_id),
                   log_template=_none(r.log_template), sender=r.container_name)
