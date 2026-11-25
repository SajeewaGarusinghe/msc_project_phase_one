"""The decision chain for one case: attribution → matcher → applicability → R1/R2 (proposal Fig 11).

``judge_case`` returns **one row per evidence item** (every span, every log line), computed once
per case so the defence wrapper can look verdicts up per tool output (P3 item 6, next step).

* attribution off is the ablation "rules without attribution" (P2-T6 needs it): every record goes
  to the matcher, and nothing reads ``sender``.
* the **claim** of a matched pair is the server span (writer D); the caller's client span is its
  **witness** (proposal §08's rule table). Rule verdicts attach to the server span only. The client
  span gets ``NO_WITNESS`` / ``witness_side``: no rule tests a client span's own claim. Report that
  detail apart from genuinely unwitnessed spans.
* residual risk, accepted and recorded: a compromised **caller** that shortens its own client span
  gets a genuine callee's server span contradicted, and M2 withholds it. §05's "at least one honest
  witness" assumption is what rules this out; FrameBench's target rule (c) never makes the target
  an upstream caller of the root cause.

``applicability_in_clean`` is the entry point for FrameBench's ``rule_applicable`` labels: it is
computed on the **clean, pre-overlay** case, so the matcher never grades its own output on an
overlay (P2 item 4; decisions log 25 Nov).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from provrca_bench.provrca import attribution as attr
from provrca_bench.provrca.matcher import MATCHED, match
from provrca_bench.provrca.rules import Config, apply_rules, combine
from provrca_bench.verdicts import VERDICT_OF, Reason

SPAN_COLUMNS = ("spanID", "parentSpanID", "serviceName", "methodName", "operationName",
                "duration", "statusCode")


def read_traces(path, *, with_sender: bool = True) -> pd.DataFrame:
    want = set(SPAN_COLUMNS) | ({"sender"} if with_sender else set())
    return pd.read_csv(path, usecols=lambda c: c in want, dtype=str, keep_default_na=False,
                       low_memory=False)


def _pairs(spans: pd.DataFrame, out: pd.DataFrame) -> pd.DataFrame:
    """The matched pairs (server rows), with both sides' durations and codes."""
    srv = out[(out.role == "server") & (out.outcome == MATCHED)]
    first = ~spans.spanID.duplicated()
    dur = pd.Series(spans.duration[first].values, index=spans.spanID[first].values)
    code = pd.Series(spans.statusCode[first].values, index=spans.spanID[first].values)
    return pd.DataFrame({
        "caller": srv.caller, "callee": srv.callee,
        "method": spans.loc[srv.index, "methodName"].astype(str).str.strip(),
        "tier": srv.tier,
        "server_us": spans.loc[srv.index, "duration"], "client_us": srv.partner.map(dur),
        "server_code": spans.loc[srv.index, "statusCode"], "client_code": srv.partner.map(code),
    }, index=srv.index)


def judge_spans(spans: pd.DataFrame, cfg: Config, *, attribution: bool = True) -> pd.DataFrame:
    for c in SPAN_COLUMNS:
        if c not in spans.columns:
            spans = spans.assign(**{c: ""})
    if attribution:
        bad = attr.rejected_spans(spans)
    else:
        bad = pd.Series(False, index=spans.index)
    ok = spans[~bad]
    m = match(ok, rejected=spans[bad])
    res = pd.DataFrame({"kind": "span", "record_id": "span:" + spans.spanID.astype(str),
                        "reason": "", "detail": "", "role": "", "tier": "", "route": "",
                        "r1_reason": "", "r1_detail": "", "r2_reason": "", "r2_detail": "",
                        "server_us": "", "client_us": "", "server_code": "", "client_code": ""},
                       index=spans.index)
    res.loc[bad, ["reason", "detail"]] = [Reason.ATTR_MISMATCH.value, "claim_not_sender"]
    res.loc[m.index, ["reason", "detail", "role", "tier"]] = m[["outcome", "detail", "role", "tier"]].values

    pairs = _pairs(ok, m)
    if len(pairs):
        rr = apply_rules(pairs, cfg)
        reason, detail = combine(rr.r1_reason, rr.r2_reason, rr.r1_detail, rr.r2_detail)
        res.loc[pairs.index, "reason"] = reason
        res.loc[pairs.index, "detail"] = detail
        res.loc[pairs.index, ["r1_reason", "r1_detail", "r2_reason", "r2_detail"]] = rr.values
        res.loc[pairs.index, "route"] = pairs.caller + ">" + pairs.callee + "/" + pairs.method
        res.loc[pairs.index, ["server_us", "client_us", "server_code", "client_code"]] = \
            pairs[["server_us", "client_us", "server_code", "client_code"]].astype(str).values
    wit = m.index[(m.role == "client") & (m.outcome == MATCHED)]
    res.loc[wit, ["reason", "detail"]] = [Reason.NO_WITNESS.value, "witness_side"]
    assert (res.reason != MATCHED).all() and (res.reason != "").all()
    res["verdict"] = res.reason.map(lambda r: VERDICT_OF[Reason(r)].value)
    return res


def judge_logs(logs: pd.DataFrame, *, attribution: bool = True) -> pd.DataFrame:
    bad = attr.rejected_logs(logs) if attribution else pd.Series(False, index=logs.index)
    res = pd.DataFrame({"kind": "log", "record_id": "log:" + logs.index.astype(str),
                        "reason": np.where(bad, Reason.ATTR_MISMATCH.value, Reason.NO_WITNESS.value),
                        "detail": np.where(bad, "claim_not_sender", attr.LOG_DETAIL)},
                       index=logs.index)
    res["verdict"] = res.reason.map(lambda r: VERDICT_OF[Reason(r)].value)
    return res


def applicability_in_clean(clean_spans: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Per genuine server span of the clean case: whether R1 / R2 apply to its pair (their verdict
    is compatible or contradicted, not unknown). FrameBench labels a forged span by the clean
    span it edits (same spanID)."""
    j = judge_spans(clean_spans, cfg, attribution=False)
    s = j[j.role == "server"]
    applied = lambda r: r.isin([Reason.R1_ENCLOSURE_FAIL.value, Reason.OK_ENCLOSED.value,  # noqa: E731
                                Reason.R2_STATUS_CONFLICT.value, Reason.OK_STATUS.value])
    return pd.DataFrame({"spanID": clean_spans.loc[s.index, "spanID"],
                         "r1_applicable": applied(s.r1_reason), "r2_applicable": applied(s.r2_reason)})
