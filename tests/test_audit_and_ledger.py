"""The verdict log and the run ledger (P1 items 6 and 7)."""
import pytest

from provrca_bench.contracts import LedgerRow, VerdictRecord
from provrca_bench.defence import AuditLog
from provrca_bench.ledger import Ledger
from provrca_bench.verdicts import Reason


def test_the_audit_log_round_trips_and_separates_the_withheld(tmp_path):
    log = AuditLog(tmp_path / "runs" / "r1" / "audit.jsonl")
    records = [
        VerdictRecord.of(run_id="r1", case_id="c1", record_id="s1", reason=Reason.R1_ENCLOSURE_FAIL,
                         rule_inputs={"client_duration": 5000, "server_duration": 7000}),
        VerdictRecord.of(run_id="r1", case_id="c1", record_id="s2", reason=Reason.OK_ENCLOSED),
        VerdictRecord.of(run_id="r1", case_id="c1", record_id="s3", reason=Reason.NO_WITNESS),
        VerdictRecord.of(run_id="r1", case_id="c1", record_id="s4", reason=Reason.ATTR_MISMATCH),
    ]
    assert log.write(records) == 4
    back = log.read()
    assert [r.record_id for r in back] == ["s1", "s2", "s3", "s4"]
    assert back[0].rule_inputs["server_duration"] == 7000
    # compatible and unknown are logged but not withheld, so P8 can audit clean removals
    assert {r.record_id for r in log.withheld()} == {"s1", "s4"}


def test_the_audit_log_appends_rather_than_truncating(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.write([VerdictRecord.of(run_id="r", case_id="c", record_id="a", reason=Reason.OK_STATUS)])
    log.write([VerdictRecord.of(run_id="r", case_id="c", record_id="b", reason=Reason.OK_STATUS)])
    assert [r.record_id for r in log.read()] == ["a", "b"]


def row(**kw):
    base = dict(run_id="r1", cell="E1/D0/clean", case_id="re2-ob-0001", arm="D0", seed=1,
                model="gemini/gemini-3.5-flash-lite-07-2026", status="planned")
    return LedgerRow(**{**base, **kw})


def test_a_run_is_planned_before_it_starts_and_updated_when_it_ends(tmp_path):
    ledger = Ledger(tmp_path / "ledger.csv")
    ledger.plan(row())
    assert [r.status for r in ledger.rows()] == ["planned"]
    ledger.update("r1", status="done", tokens_in=61243, cost_usd=0.0125)
    done = ledger.rows()[0]
    assert done.status == "done" and done.cost_usd == 0.0125


def test_planning_the_same_run_twice_is_refused(tmp_path):
    ledger = Ledger(tmp_path / "ledger.csv")
    ledger.plan(row())
    with pytest.raises(ValueError, match="already in the ledger"):
        ledger.plan(row())


def test_updating_an_unplanned_run_is_refused(tmp_path):
    ledger = Ledger(tmp_path / "ledger.csv")
    with pytest.raises(KeyError, match="plan it before updating"):
        ledger.update("nope", status="done")


def test_the_cost_cap_stops_the_study_not_just_a_run(tmp_path):
    ledger = Ledger(tmp_path / "ledger.csv")
    ledger.plan(row(run_id="a"))
    ledger.update("a", status="done", cost_usd=690.0)
    ledger.check_cap(1000.0)
    with pytest.raises(RuntimeError, match="cost cap reached"):
        ledger.check_cap(690.0)
