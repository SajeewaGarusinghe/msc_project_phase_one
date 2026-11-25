"""The one-to-one attempt matcher, the second link of the decision chain (build guide P3 item 2).

A rewrite, with tests, of G0 stage 2's role inference and attempt matching
(``g0_pilot/stage2/audit_traces.py`` and ``pairs.py``; ``09-results/g0/02-role-inference.md``,
``03-attempt-matching.md``). It must reproduce G0's published counts exactly on clean data, and it
adds what G0 did not need: **every span receives exactly one outcome**, so no record reaches the
agent without a named exit (A3, "one line per evidence item").

**Writer domain is the claimed ``serviceName``, never the ``sender`` attribute.** Attribution is
the only reader of ``sender`` (``attribution.py``). With attribution on, the two are equal on every
surviving span, so nothing is lost; with attribution off, reading ``sender`` here would quietly do
attribution inside the matcher and break the layer ablation (proposal §05, build-guide P2-T6).
``MATCH_COLUMNS`` is the whole input, and a test checks that ``sender`` cannot change the output.

**Uniqueness before assertion.** A pair is asserted only when exactly one client and one server
are linked by the propagated span id; anything else is *unknown* with a reason, never a
nearest-candidate guess. This is the discipline ExecWeave (arXiv 2610.21146) uses for its bridges;
it is credited there (K25).

Outcomes, by precedence (the first that applies wins):

1. the span's own id occurs more than once ............................ ``AMB_DUPLICATE`` (dup_id)
2. the span names itself as its parent ................................ ``AMB_SELF`` (self_parent)
3. **server**: its parent is present and belongs to another service:
   the parent's id is duplicated ...................................... ``AMB_DUPLICATE`` (partner_dup)
   the parent has several cross-service children, one call ........... ``AMB_REUSED_CTX``
   ... several calls .................................................. ``AMB_NON_1TO1``
   role tier ``NA_ROLE`` .............................................. ``NA_ROLE``
   (caller_parent_rejected / caller_parent_absent when check (b) failed for that reason)
   otherwise .......................................................... ``MATCHED`` (tier kept)
4. **client**: it has cross-service children -- mirrors its single edge's outcome, or an
   ``AMB_*`` code when it has several
5. its parent id is absent from the file .............................. ``NO_WITNESS``
   (partner_rejected if attribution removed that id, else partner_absent: sampling loss)
6. an RPC-named span (methodName, or an HTTP verb as operation) with no
   cross-service child ................................................ ``NO_WITNESS``
   (partner_rejected, else unpaired_rpc). The detail stays neutral on purpose: genuine calls to
   cartservice, adservice and shippingservice land here, but so does a forgery that dropped its
   trace context (T6, F2c/F3c), so it must not read as "the callee emits no spans". The
   ``hint`` column records the operation-name form (G0: a callee-side gRPC operation carries
   '/' or 'grpc.'), as a hint only, never a verdict
7. a root span ........................................................ ``NO_WITNESS`` (root)
8. a same-service child ............................................... ``AMB_SELF`` (internal)

A span that is both a server and a client keeps its server outcome, with ``also_client`` noted.
``MATCHED`` is internal to ProvRCA, not an A3 reason code: a matched pair goes on to the
applicability table and R1/R2 (later P3 steps).

The role tiers are G0 step 2's, kept in the output rather than collapsed: ``STRONG``,
``ROOT_CLIENT`` (the client is a trace root, so check (b) cannot be evaluated) and
``WEAK_VERB_ONLY`` (Train Ticket: the client names only the HTTP verb).
"""
from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd

from provrca_bench.verdicts import Reason

MATCH_COLUMNS = ("spanID", "parentSpanID", "serviceName", "methodName", "operationName")
HTTP_VERBS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")
MATCHED = "MATCHED"
TIERS = ("STRONG", "ROOT_CLIENT", "WEAK_VERB_ONLY", "NA_ROLE")


def _clean(s: pd.Series) -> pd.Series:
    """Strings with every flavour of empty (NaN, None, '', 'nan', whitespace) mapped to ''."""
    s = s.astype(object).where(s.notna(), "").astype(str).str.strip()
    return s.where(s.str.lower() != "nan", "")


def _op_norm(s: pd.Series) -> pd.Series:
    """G0's normalisation: callee-side gRPC operations carry a leading '/' or 'grpc.' prefix."""
    s = s.str.replace(r"^/", "", regex=True)
    return s.str.replace(r"^grpc\.", "", regex=True)


def _op_tier(c_op: pd.Series, s_op: pd.Series) -> pd.Series:
    """G0's ``op_tier``: exact / norm_equal / verb_only_consistent / mismatch / unavailable."""
    out = pd.Series("mismatch", index=c_op.index, dtype=object)
    verb = c_op.isin(HTTP_VERBS) & np.array(
        [b.startswith(a + " ") for a, b in zip(c_op, s_op)], dtype=bool)
    out[verb] = "verb_only_consistent"
    out[_op_norm(c_op) == _op_norm(s_op)] = "norm_equal"
    out[c_op == s_op] = "exact"
    out[(c_op == "") | (s_op == "")] = "unavailable"
    return out


def match(spans: pd.DataFrame, rejected: pd.DataFrame | None = None) -> pd.DataFrame:
    """One outcome row per input span, aligned to ``spans``' index.

    ``spans`` are the spans that passed attribution; ``rejected`` (optional) are the ones it
    removed, given only so an outcome can say *why* a witness is missing (its ``spanID`` and
    ``parentSpanID`` are read, nothing else). Columns of the result: ``spanID``, ``role``
    (server / client / none), ``outcome`` (``MATCHED`` or an A3 reason code), ``detail``,
    ``tier``, ``partner`` (the other side's span id), ``caller`` and ``callee`` (service names),
    ``hint`` (for unpaired RPC spans: ``server_form`` or ``client_form``), and ``parent``
    (``none`` / ``in_file`` / ``absent``: whether the span's parent id is in the file, whatever
    its outcome -- G0's dangling-parent count is the ``absent`` total).
    """
    df = pd.DataFrame(index=spans.index)
    for c in MATCH_COLUMNS:
        df[c] = _clean(spans[c]) if c in spans.columns else ""
    sid, pid, svc = df.spanID, df.parentSpanID, df.serviceName
    meth, op = df.methodName, df.operationName
    rej_ids: set[str] = set()
    rej_parents: set[str] = set()
    if rejected is not None and len(rejected):
        rej_ids = set(_clean(rejected["spanID"]))
        rej_parents = set(_clean(rejected["parentSpanID"])) - {""}

    dup = sid.duplicated(keep=False)
    dup_ids = set(sid[dup])
    first = ~sid.duplicated()
    svc_of = pd.Series(svc[first].values, index=sid[first].values)
    meth_of = pd.Series(meth[first].values, index=sid[first].values)
    op_of = pd.Series(op[first].values, index=sid[first].values)
    par_of = pd.Series(pid[first].values, index=sid[first].values)

    has_parent = pid != ""
    self_parent = has_parent & (pid == sid)
    p_svc = pid.map(svc_of)
    parent_present = has_parent & p_svc.notna()
    is_server = parent_present & ~self_parent & (p_svc != svc)
    call = meth.where(meth != "", op)

    # cross-service children per client span id
    kids = pd.DataFrame({"client": pid[is_server], "call": call[is_server]})
    nkids = kids.groupby("client").size()
    ncalls = kids.groupby("client")["call"].nunique()
    is_client = sid.isin(nkids.index)

    out = pd.DataFrame({"spanID": sid, "role": "none", "outcome": "", "detail": "",
                        "tier": "", "partner": "", "caller": "", "callee": "", "hint": "",
                        "parent": np.where(~has_parent, "none",
                                           np.where(p_svc.notna(), "in_file", "absent"))},
                       index=df.index)
    todo = pd.Series(True, index=df.index)

    def assign(mask: pd.Series, outcome: str, detail: str | pd.Series = "") -> None:
        m = mask & todo
        out.loc[m, "outcome"] = outcome
        out.loc[m, "detail"] = detail[m] if isinstance(detail, pd.Series) else detail
        todo[m] = False

    assign(dup, Reason.AMB_DUPLICATE.value, "dup_id")
    assign(self_parent, Reason.AMB_SELF.value, "self_parent")

    # 3. server side: role tier exactly as G0 step 2 defines it
    srv = is_server & todo
    c_meth, c_op = pid.map(meth_of), pid.map(op_of)
    meth_unavail = (c_meth == "") | (meth == "")
    meth_match = ~meth_unavail & (c_meth == meth)
    tier_op = _op_tier(c_op.where(srv, ""), op.where(srv, ""))
    gp = pid.map(par_of)
    caller_root = gp == ""
    caller_same = gp.map(svc_of) == p_svc
    named = tier_op.isin(["exact", "norm_equal"]) & (meth_match | meth_unavail)
    tier = pd.Series("NA_ROLE", index=df.index, dtype=object)
    tier[named & caller_same] = "STRONG"
    tier[named & caller_root] = "ROOT_CLIENT"
    tier[(tier_op == "verb_only_consistent") & (caller_same | caller_root) & ~named] = "WEAK_VERB_ONLY"

    n_of_parent = pid.map(nkids).fillna(0)
    one_call = pid.map(ncalls).fillna(0) == 1
    out.loc[srv, ["role", "tier", "partner", "caller", "callee"]] = np.column_stack(
        [np.full(int(srv.sum()), "server", dtype=object), tier[srv], pid[srv], p_svc[srv], svc[srv]])
    assign(srv & pid.isin(dup_ids), Reason.AMB_DUPLICATE.value, "partner_dup")
    assign(srv & (n_of_parent > 1) & one_call, Reason.AMB_REUSED_CTX.value)
    assign(srv & (n_of_parent > 1), Reason.AMB_NON_1TO1.value)
    gp_gone = (gp != "") & gp.map(svc_of).isna()
    na_detail = pd.Series(np.where(gp_gone & gp.isin(rej_ids), "caller_parent_rejected",
                                   np.where(gp_gone, "caller_parent_absent", "")), index=df.index)
    assign(srv & (tier == "NA_ROLE"), Reason.NA_ROLE.value, na_detail)
    assign(srv, MATCHED)
    out.loc[srv & is_client, "detail"] = (out.detail[srv & is_client] + " also_client").str.strip()

    # 4. client side: mirror the single edge, or ambiguous when there are several
    cli = is_client & todo
    if cli.any():
        many = cli & (sid.map(nkids) > 1)
        assign(many & sid.map(ncalls).eq(1), Reason.AMB_REUSED_CTX.value)
        assign(many, Reason.AMB_NON_1TO1.value)
        single = cli & todo
        srv_rows = out[is_server].assign(client=pid[is_server])
        srv_rows = srv_rows[~srv_rows.client.duplicated(keep=False)].set_index("client")
        mirror = srv_rows.reindex(sid[single].values)
        mirror.index = sid[single].index
        det = mirror.detail.replace({"dup_id": "partner_dup"}).str.replace(
            "also_client", "", regex=False).str.strip()
        out.loc[single, "outcome"] = mirror.outcome
        out.loc[single, "detail"] = det
        out.loc[single, ["tier", "caller", "callee"]] = mirror[["tier", "caller", "callee"]]
        out.loc[single, "partner"] = mirror.spanID
        todo[single] = False
        out.loc[cli, "role"] = "client"
        multi_kids = cli & (sid.map(nkids) > 1)
        out.loc[multi_kids, "caller"] = svc[multi_kids]

    # 5-8. no partner in the file
    witness_gone = pd.Series(np.where(pid.isin(rej_ids), "partner_rejected", "partner_absent"),
                             index=df.index)
    assign(has_parent & ~parent_present, Reason.NO_WITNESS.value, witness_gone)
    rpc_named = (meth != "") | op.isin(HTTP_VERBS)
    callee_gone = pd.Series(np.where(sid.isin(rej_parents), "partner_rejected",
                                     "unpaired_rpc"), index=df.index)
    unpaired = rpc_named & todo
    out.loc[unpaired, "hint"] = np.where(op[unpaired].str.match(r"^(/|grpc\.)"),
                                         "server_form", "client_form")
    assign(rpc_named, Reason.NO_WITNESS.value, callee_gone)
    assign(~has_parent, Reason.NO_WITNESS.value, "root")
    assign(pd.Series(True, index=df.index), Reason.AMB_SELF.value, "internal")
    return out


def matched_pairs(outcomes: pd.DataFrame) -> pd.DataFrame:
    """The matched attempts, one row per pair, seen from the server side."""
    s = outcomes[(outcomes.role == "server") & (outcomes.outcome == MATCHED)]
    return s.rename(columns={"spanID": "server_span", "partner": "client_span"})[
        ["client_span", "server_span", "caller", "callee", "tier"]]


def summarise(outcomes: pd.DataFrame, by: Iterable[str] = ("role", "outcome", "detail")) -> pd.Series:
    return outcomes.groupby(list(by), dropna=False).size()


SERVER_CODE = {"STRONG": "MATCHED_1TO1", "ROOT_CLIENT": "MATCHED_1TO1_ROOT_CLIENT",
               "WEAK_VERB_ONLY": "MATCHED_1TO1_WEAK_ROLE"}


def read_spans(traces_csv, *, extra: Iterable[str] = ()) -> pd.DataFrame:
    """The columns the matcher (and, if asked, attribution) needs, every value as a string."""
    want = set(MATCH_COLUMNS) | set(extra)
    return pd.read_csv(traces_csv, usecols=lambda c: c in want, dtype=str,
                       keep_default_na=False, low_memory=False)


def census(outcomes: pd.DataFrame) -> dict:
    """The counts G0 published, recomputed from outcomes, plus the per-span totals.

    ``server`` is keyed like G0's ``matching_counts.csv`` -- (caller, callee, code) with G0's
    MATCHED_* names -- and is the like-for-like parity check: G0 counted cross-service edges,
    which are exactly the server-role rows here. The other keys are per-span counts.
    """
    s = outcomes[outcomes.role == "server"]
    code = s.outcome.where(s.outcome != MATCHED, s.tier.map(SERVER_CODE))
    server = {k: int(v) for k, v in s.assign(code=code).groupby(["caller", "callee", "code"]).size().items()}
    det = outcomes.groupby(["outcome", "detail"]).size()
    get = lambda o, d: int(det.get((o, d), 0))  # noqa: E731
    return {"server": server,
            "spans": len(outcomes),
            "partner_absent": get(Reason.NO_WITNESS.value, "partner_absent"),
            "dangling_parent": int((outcomes.parent == "absent").sum()),
            "unpaired_rpc": get(Reason.NO_WITNESS.value, "unpaired_rpc"),
            "root": get(Reason.NO_WITNESS.value, "root"),
            "internal": get(Reason.AMB_SELF.value, "internal"),
            "by_role_outcome": {"|".join(map(str, k)): int(v) for k, v in
                                outcomes.groupby(["role", "outcome", "detail"]).size().items()}}
