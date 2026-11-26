"""Typed contracts for everything that crosses a boundary (build-guide appendix A3).

Four artefacts have schemas because four readers disagree about what they may see: the agent and
the arms read case rows, the scorer alone reads labels, analysis reads verdicts and the ledger.
The schemas also carry the two converter constraints A3 calls out, because they bind overlays and
a violation is silent otherwise:

  * a service name must contain no ``_`` -- RCAEval's methods take the service as the text before
    the first underscore (``RCAEval/convert/common.py: check_service``);
  * ``tracets_lat`` marks a span slow against the mean plus three standard deviations of that
    operation's spans *before* the failure start, so a row placed before the fault time shifts the
    baseline. Overlays therefore write inside the post-fault window unless the family needs
    otherwise (F4), which ``TraceSpanRow.in_post_fault_window`` lets P2 check.

Column names and types follow the released RE2/RE3 files as measured in G0 stage 2 step 1, not an
idealisation of them: ``statusCode`` really is absent on some genuine spans, and ``methodName`` is
empty on the frontend's HTTP spans.

**Corrected 25 Nov 2026 against the real files** (``tests/test_contracts_real_data.py``). The P1
versions were tested on hand-written rows only, and two fields were wrong: ``time`` is wall-clock
``HH:MM`` text in both ``traces.csv`` and ``logs.csv``, not Unix seconds, and a log row's
``timestamp`` is integer **nanoseconds**. ``statusCode`` is written as a float (``0.0``, ``13.0``),
which an integer field accepts when it has no fractional part.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from provrca_bench.verdicts import Reason, Verdict, verdict_for

ServiceName = Annotated[str, Field(min_length=1)]


def _no_underscore(name: str) -> str:
    if "_" in name:
        raise ValueError(
            f"service name {name!r} must contain no '_': RCAEval's methods read the service as "
            "the text before the first underscore (A3)"
        )
    return name


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class TraceSpanRow(_Strict):
    """One row of ``traces.csv``, plus the sender attribute every record carries (A4)."""

    time: str = Field(pattern=r"^\d{1,2}:\d{2}$",
                      description="wall-clock HH:MM, as the released files record it")
    traceID: str = Field(min_length=1)
    spanID: str = Field(min_length=1)
    serviceName: ServiceName
    methodName: str | None = Field(
        default=None, description="absent on the frontend's HTTP spans (step 1)")
    operationName: str | None = None
    startTimeMillis: int = Field(ge=0)
    startTime: int = Field(ge=0, description="microseconds")
    duration: int = Field(ge=0, description="microseconds")
    statusCode: int | None = Field(
        default=None, description="the protocol status code; absent on some genuine spans (K19)")
    parentSpanID: str | None = None
    sender: ServiceName = Field(
        description="who wrote the record: synthetic on Track A, collector-enforced on Track B")

    _check_service = field_validator("serviceName", "sender")(_no_underscore)

    @model_validator(mode="after")
    def _start_fields_agree(self) -> "TraceSpanRow":
        if abs(self.startTime // 1000 - self.startTimeMillis) > 1:
            raise ValueError(
                f"startTime ({self.startTime} us) and startTimeMillis ({self.startTimeMillis}) "
                "disagree by more than a millisecond")
        return self

    def in_post_fault_window(self, inject_time_unix_s: int) -> bool:
        """Whether this row sits at or after the fault start, which all families but F4 require."""
        return self.startTime >= inject_time_unix_s * 1_000_000


class LogLineRow(_Strict):
    """One row of ``logs.csv``, plus the sender attribute.

    ``cluster_id`` and ``log_template`` are added by ``rcaeval-convert derive``; an injected line
    that matches no existing template creates a new one, which is itself detectable (K22).
    """

    time: str = Field(pattern=r"^\d{1,2}:\d{2}$", description="wall-clock HH:MM")
    timestamp: int = Field(ge=0, description="nanoseconds since the epoch")
    container_name: ServiceName
    message: str
    level: str | None = None
    req_path: str | None = None
    error: str | None = None
    cluster_id: int | None = None
    log_template: str | None = None
    sender: ServiceName

    _check_service = field_validator("container_name", "sender")(_no_underscore)


class LabelRecord(_Strict):
    """A sealed label (A3). **Scorer only** -- never inside a case directory, never read by an arm.

    The three per-record labels are deliberately separate: a record can be fabricated and yet not
    factually false (A1's volume control), and it can be factually false and still out of a rule's
    reach (which is what the coverage ceiling measures).
    """

    case_id: str = Field(min_length=1)
    variant: str = Field(min_length=1, description="family-threat-dose-seed, as the directory name")
    target: ServiceName
    family: Literal["F1", "F2", "F3", "F4", "FS", "F2c", "F3c", "FSc", "F2cD", "A1", "AF1", "AF2"]
    threat_model: Literal["T1", "T2"]
    dose: str = Field(min_length=1, description="high or low, fixed from the P0 pilot")
    seed: int
    record_id: str = Field(min_length=1, description="the spanID or log row this label is about")
    fabricated: bool = Field(description="always true for an injected record")
    factually_false: bool = Field(
        description="its claim disagrees with the original data for the same request or window")
    rule_applicable: bool | None = Field(
        description="as delivered: the rule its forgery targets (F2: R2, F3/F3c: R1, FS: either) "
                    "has its conditions hold for the record's pair in the overlay. Derived from "
                    "what the generator did, never by running the matcher on the overlay. None "
                    "for records that are not claims (a T1 forged caller span is a witness)")
    rule_applicable_clean: bool | None = Field(
        default=None,
        description="the same rule's conditions on the clean, pre-overlay pair "
                    "(provrca.chain.applicability_in_clean); for a T1 forged pair, on the "
                    "constructed pair judged with attribution off")
    escape: Literal["none", "R1_cap", "rule_not_applicable", "witness", "no_log_rule",
                    "witness_unlinked", "code_range"] | None = Field(
        default=None, description="how the forgery stays out of a rule's reach, if it does: "
                    "R1_cap (F3c: under the caller's wait), rule_not_applicable (no rule for the "
                    "clean pair), witness_unlinked (F2c/FSc: parentSpanID replaced by an id not in "
                    "the file), code_range (F2cD, dev-only: a status outside gRPC's 0-16)")

    _check_service = field_validator("target")(_no_underscore)

    @model_validator(mode="after")
    def _injected_records_are_fabricated(self) -> "LabelRecord":
        if not self.fabricated:
            raise ValueError("a label record describes an injected record, so fabricated is always true")
        return self


class VerdictRecord(_Strict):
    """One line of ``runs/<run>/audit.jsonl`` (A3): a decision, its reason, and the rule's inputs.

    The verdict is derived from the reason code rather than supplied, so a caller cannot record a
    decision A3 does not permit for that code.
    """

    run_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    record_id: str = Field(min_length=1)
    reason: Reason
    verdict: Verdict
    rule_inputs: dict[str, float | int | str | None] = Field(default_factory=dict)
    note: str | None = Field(default=None, description="the one line shown when an item is withheld")

    @model_validator(mode="after")
    def _verdict_matches_reason(self) -> "VerdictRecord":
        expected = verdict_for(self.reason)
        if self.verdict != expected:
            raise ValueError(
                f"reason {self.reason} is a {expected} verdict in A3, not {self.verdict}")
        return self

    @classmethod
    def of(cls, *, run_id: str, case_id: str, record_id: str, reason: Reason, **kw) -> "VerdictRecord":
        """Build a record with the verdict A3 assigns to `reason`."""
        return cls(run_id=run_id, case_id=case_id, record_id=record_id, reason=reason,
                   verdict=verdict_for(reason), **kw)


class LedgerRow(_Strict):
    """One row of ``ledger.csv`` (A3). Written before a run starts, updated when it ends."""

    run_id: str = Field(min_length=1)
    cell: str = Field(min_length=1, description="the experiment cell, e.g. 'E1/D0/F2-high'")
    case_id: str = Field(min_length=1)
    arm: Literal["D0", "D1", "D2", "D3", "D4"]
    seed: int
    model: str = Field(min_length=1, description="a dated version, never an alias")
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    status: Literal["planned", "running", "done", "failed", "skipped"]

    @field_validator("model")
    @classmethod
    def _no_floating_alias(cls, model: str) -> str:
        if model.endswith(("latest", "-preview")):
            raise ValueError(f"model {model!r} is a floating alias; record a dated version (section 13)")
        return model


class SplitEntry(_Strict):
    """One template's split assignment in ``manifest/split.json`` (A3), frozen in P1."""

    template: str = Field(min_length=1, description="<service>_<fault>, the case directory name")
    split: Literal["dev", "S1", "S2", "S3"]
    cases: list[str] = Field(min_length=1)
    file_sha256: dict[str, str] = Field(
        default_factory=dict, description="case-relative path -> SHA-256, hashed at P1")
