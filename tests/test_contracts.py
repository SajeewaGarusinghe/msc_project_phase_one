"""The A3 schemas, including the two converter constraints that bind overlays."""
import pytest
from pydantic import ValidationError

from provrca_bench.contracts import (LabelRecord, LedgerRow, LogLineRow, SplitEntry,
                                     TraceSpanRow, VerdictRecord)
from provrca_bench.verdicts import Reason, Verdict

INJECT_TIME = 1_705_354_000


def span(**kw):
    base = dict(time="18:35", traceID="abc123", spanID="s1",
                serviceName="productcatalogservice", methodName="GetProduct",
                operationName="hipstershop.ProductCatalogService/GetProduct",
                startTimeMillis=1_705_354_100_000, startTime=1_705_354_100_000_000,
                duration=1500, statusCode=0, parentSpanID="s0", sender="productcatalogservice")
    return TraceSpanRow(**{**base, **kw})


def test_a_well_formed_span_validates():
    assert span().duration == 1500


def test_a_service_name_with_an_underscore_is_refused():
    # RCAEval reads the service as the text before the first underscore, so this is not cosmetic
    with pytest.raises(ValidationError, match="must contain no"):
        span(serviceName="unknown_service")
    with pytest.raises(ValidationError, match="must contain no"):
        span(sender="attacker_pod")


def test_start_fields_must_agree():
    with pytest.raises(ValidationError, match="disagree"):
        span(startTimeMillis=1_705_354_999_000)


def test_absent_method_and_status_are_allowed_because_genuine_spans_have_them():
    row = span(methodName=None, statusCode=None)
    assert row.methodName is None and row.statusCode is None


def test_post_fault_window_check():
    assert span().in_post_fault_window(INJECT_TIME) is True
    early = span(startTime=(INJECT_TIME - 60) * 1_000_000,
                 startTimeMillis=(INJECT_TIME - 60) * 1000)
    assert early.in_post_fault_window(INJECT_TIME) is False


def test_extra_columns_are_refused():
    with pytest.raises(ValidationError):
        span(family="F2")          # a family name must never reach a case row


def test_log_row_validates_and_refuses_an_underscore_container():
    row = LogLineRow(time="21:24", timestamp=1_705_354_100_000_000_000, container_name="frontend",
                     message="request complete", level="info", sender="frontend")
    assert row.level == "info"
    with pytest.raises(ValidationError):
        LogLineRow(time="21:24", timestamp=1, container_name="front_end", message="m", sender="frontend")


def label(**kw):
    base = dict(case_id="re2-ob-0001", variant="F2-T2-high-7", target="paymentservice",
                family="F2", threat_model="T2", dose="high", seed=7, record_id="s9",
                fabricated=True, factually_false=True, rule_applicable=True)
    return LabelRecord(**{**base, **kw})


def test_a_label_record_validates():
    assert label().family == "F2"


def test_an_injected_record_is_always_fabricated():
    with pytest.raises(ValidationError, match="if and only if"):
        label(fabricated=False)


@pytest.mark.parametrize("family,escape", [("AF1", "no_time_rule"), ("AF2", "identity_claim")])
def test_an_honest_fault_is_never_fabricated(family, escape):
    """Two-way: AF1/AF2 records are genuine records with a false time or writer, made by no
    attacker (step 5 review, item 8)."""
    lab = label(family=family, fabricated=False, rule_applicable=False,
                rule_applicable_clean=False, escape=escape)
    assert not lab.fabricated
    with pytest.raises(ValidationError, match="if and only if"):
        label(family=family, fabricated=True, rule_applicable=False, escape=escape)


def test_the_three_labels_are_independent():
    # A1 adds records that are fabricated but not factually false; a record can be false and still
    # out of a rule's reach, which is what the coverage ceiling measures
    assert label(factually_false=False, rule_applicable=False).fabricated


def test_an_unknown_family_is_refused():
    with pytest.raises(ValidationError):
        label(family="F9")


def test_a_verdict_must_match_its_reason_code():
    ok = VerdictRecord.of(run_id="r1", case_id="c1", record_id="s1",
                          reason=Reason.R1_ENCLOSURE_FAIL)
    assert ok.verdict is Verdict.CONTRADICTED
    with pytest.raises(ValidationError, match="is a contradicted verdict"):
        VerdictRecord(run_id="r1", case_id="c1", record_id="s1",
                      reason=Reason.R1_ENCLOSURE_FAIL, verdict=Verdict.COMPATIBLE)


def test_the_ledger_refuses_a_floating_model_alias():
    base = dict(run_id="r1", cell="E1/D0/F2-high", case_id="c1", arm="D0", seed=1,
                status="planned")
    assert LedgerRow(model="gemini/gemini-3.5-flash-lite-07-2026", **base).arm == "D0"
    with pytest.raises(ValidationError, match="floating alias"):
        LedgerRow(model="gemini/gemini-flash-latest", **base)


def test_split_entry_requires_cases():
    entry = SplitEntry(template="checkoutservice_cpu", split="dev", cases=["re2-ob-0001"])
    assert entry.split == "dev"
    with pytest.raises(ValidationError):
        SplitEntry(template="t", split="dev", cases=[])


def test_a_split_must_be_one_of_the_four():
    with pytest.raises(ValidationError):
        SplitEntry(template="t", split="S4", cases=["c"])
