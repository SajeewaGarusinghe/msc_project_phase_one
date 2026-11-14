"""A3's verdict table, checked as data rather than trusted as prose."""
import pytest

from provrca_bench.verdicts import DEMOTED, Reason, Verdict, VERDICT_OF, is_demoted, verdict_for

# transcribed from build-guide appendix A3, independently of VERDICT_OF, so that a change to one
# has to be made deliberately in the other
A3_TABLE = {
    "ATTR_MISMATCH": "rejected",
    "R1_ENCLOSURE_FAIL": "contradicted",
    "R2_STATUS_CONFLICT": "contradicted",
    "OK_ENCLOSED": "compatible",
    "OK_STATUS": "compatible",
    "AMB_DUPLICATE": "unknown",
    "AMB_SELF": "unknown",
    "AMB_REUSED_CTX": "unknown",
    "AMB_NON_1TO1": "unknown",
    "NA_STREAMING": "unknown",
    "NA_ROLE": "unknown",
    "NA_BOUNDARY": "unknown",
    "NA_STATUS_LOSSY": "unknown",
    "NO_WITNESS": "unknown",
}


def test_every_reason_code_is_in_a3_and_maps_to_the_same_verdict():
    assert {r.value for r in Reason} == set(A3_TABLE)
    for code, verdict in A3_TABLE.items():
        assert verdict_for(Reason(code)) == verdict, code


def test_every_reason_code_has_a_verdict():
    assert set(VERDICT_OF) == set(Reason)


def test_only_rejected_and_contradicted_are_demoted():
    assert DEMOTED == frozenset({Verdict.REJECTED, Verdict.CONTRADICTED})
    for reason in Reason:
        expected = A3_TABLE[reason.value] in ("rejected", "contradicted")
        assert is_demoted(reason) is expected, reason


def test_compatible_is_not_demoted_because_compatible_is_not_true():
    # F2c and F3c are built to pass the rules; withholding compatible items would hide them
    assert not is_demoted(Reason.OK_ENCLOSED)
    assert not is_demoted(Reason.OK_STATUS)


def test_unknown_is_never_demoted():
    for reason in Reason:
        if verdict_for(reason) is Verdict.UNKNOWN:
            assert not is_demoted(reason)


def test_an_unlisted_code_is_rejected():
    with pytest.raises(ValueError):
        verdict_for("NOT_A_CODE")
