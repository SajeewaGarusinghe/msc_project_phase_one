"""Verdicts and reason codes, exactly as build-guide appendix A3 defines them.

Every ProvRCA decision is a verdict with a reason code, and the mapping from code to verdict is
fixed by A3 rather than by the caller. Only `REJECTED` and `CONTRADICTED` evidence is demoted;
`COMPATIBLE` is not "true" (F2c and F3c are built to pass), and `UNKNOWN` must stay visible in the
reported category rates rather than being folded into either side.
"""
from __future__ import annotations

from enum import StrEnum


class Verdict(StrEnum):
    REJECTED = "rejected"
    CONTRADICTED = "contradicted"
    COMPATIBLE = "compatible"
    UNKNOWN = "unknown"


class Reason(StrEnum):
    # rejected - attribution
    ATTR_MISMATCH = "ATTR_MISMATCH"
    # contradicted - a rule applied and failed
    R1_ENCLOSURE_FAIL = "R1_ENCLOSURE_FAIL"
    R2_STATUS_CONFLICT = "R2_STATUS_CONFLICT"
    # compatible - a rule applied and held
    OK_ENCLOSED = "OK_ENCLOSED"
    OK_STATUS = "OK_STATUS"
    # unknown - the attempt could not be matched one to one
    AMB_DUPLICATE = "AMB_DUPLICATE"
    AMB_SELF = "AMB_SELF"
    AMB_REUSED_CTX = "AMB_REUSED_CTX"
    AMB_NON_1TO1 = "AMB_NON_1TO1"
    # unknown - an applicability condition does not hold
    NA_STREAMING = "NA_STREAMING"
    NA_ROLE = "NA_ROLE"
    NA_BOUNDARY = "NA_BOUNDARY"
    NA_STATUS_LOSSY = "NA_STATUS_LOSSY"
    NO_WITNESS = "NO_WITNESS"


#: A3's table, as data. The verdict is a property of the reason code, not a caller's choice.
VERDICT_OF: dict[Reason, Verdict] = {
    Reason.ATTR_MISMATCH: Verdict.REJECTED,
    Reason.R1_ENCLOSURE_FAIL: Verdict.CONTRADICTED,
    Reason.R2_STATUS_CONFLICT: Verdict.CONTRADICTED,
    Reason.OK_ENCLOSED: Verdict.COMPATIBLE,
    Reason.OK_STATUS: Verdict.COMPATIBLE,
    Reason.AMB_DUPLICATE: Verdict.UNKNOWN,
    Reason.AMB_SELF: Verdict.UNKNOWN,
    Reason.AMB_REUSED_CTX: Verdict.UNKNOWN,
    Reason.AMB_NON_1TO1: Verdict.UNKNOWN,
    Reason.NA_STREAMING: Verdict.UNKNOWN,
    Reason.NA_ROLE: Verdict.UNKNOWN,
    Reason.NA_BOUNDARY: Verdict.UNKNOWN,
    Reason.NA_STATUS_LOSSY: Verdict.UNKNOWN,
    Reason.NO_WITNESS: Verdict.UNKNOWN,
}

#: Only these two verdicts are withheld from the agent (A3). Demotion under M2 removes the item
#: and says one line about why; under M1 it stays with an annotation.
DEMOTED: frozenset[Verdict] = frozenset({Verdict.REJECTED, Verdict.CONTRADICTED})


def verdict_for(reason: Reason) -> Verdict:
    """The verdict A3 assigns to this reason code."""
    return VERDICT_OF[Reason(reason)]


def is_demoted(reason: Reason) -> bool:
    """Whether evidence carrying this reason code is withheld from the agent."""
    return verdict_for(reason) in DEMOTED
