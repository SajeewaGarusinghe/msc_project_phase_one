"""P3 step 1: attribution and the one-to-one matcher (build guide P3 items 1-2).

Three layers of evidence, kept apart:

* **Constructed cases** -- one per outcome, including retries/fan-out and the T1 shapes. G0 found no
  ``AMB_*`` case at all in RCAEval (``03-attempt-matching.md``), so these branches can only be
  exercised on constructed inputs.
* **Properties** -- every span gets exactly one outcome; row order does not matter; the matcher
  never reads ``sender``.
* **Real data** (skipped when absent, e.g. on CI) -- the server side reproduces G0's published
  per-case, per-(caller, callee, code) counts on the 36 RE2-OB development cases and RE3-TT, and
  the FrameBench T2 overlays change neither attribution nor matching.
"""
from __future__ import annotations

import ast
import csv
import json
import os
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd
import pytest

from provrca_bench.provrca import attribution as attr
from provrca_bench.provrca import matcher
from provrca_bench.provrca.matcher import MATCHED, census, match, matched_pairs, read_spans

REPO = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures" / "g0"


def S(sid, parent="", svc="frontendservice", meth="", op="", sender=None):
    return {"spanID": sid, "parentSpanID": parent, "serviceName": svc, "methodName": meth,
            "operationName": op, "sender": sender or svc}


def frame(*rows):
    return pd.DataFrame(list(rows))


def by_id(out):
    """Outcome row for each unique span id."""
    return {r.spanID: r for r in out.itertuples()}


GP = "hipstershop.ProductCatalogService/GetProduct"
CONV = "hipstershop.CurrencyService/Convert"


# --------------------------------------------------------------- constructed cases
def test_g0_worked_example():
    """``02-role-inference.md``'s worked example, by hand."""
    t = frame(S("926d", "", "frontendservice", "", "HTTP GET /product"),
              S("69fd", "926d", "frontendservice", "GetProduct", GP),
              S("6c0f", "69fd", "productcatalogservice", "GetProduct", "/" + GP),
              S("c5c0", "926d", "frontendservice", "GetSupportedCurrencies",
                "hipstershop.CurrencyService/GetSupportedCurrencies"),
              S("4288", "c5c0", "currencyservice", "GetSupportedCurrencies",
                "grpc.hipstershop.CurrencyService/GetSupportedCurrencies"),
              S("6d7f", "926d", "frontendservice", "GetCart", "hipstershop.CartService/GetCart"))
    o = by_id(match(t))
    assert (o["6c0f"].role, o["6c0f"].outcome, o["6c0f"].tier, o["6c0f"].partner) == \
        ("server", MATCHED, "STRONG", "69fd")
    assert (o["69fd"].role, o["69fd"].outcome, o["69fd"].partner) == ("client", MATCHED, "6c0f")
    assert (o["4288"].outcome, o["4288"].tier) == (MATCHED, "STRONG")
    assert (o["6d7f"].outcome, o["6d7f"].detail, o["6d7f"].hint) == \
        ("NO_WITNESS", "unpaired_rpc", "client_form")
    assert (o["926d"].outcome, o["926d"].detail) == ("NO_WITNESS", "root")
    pairs = matched_pairs(match(t))
    assert set(zip(pairs.client_span, pairs.server_span)) == {("69fd", "6c0f"), ("c5c0", "4288")}


def test_root_client_tier():
    o = by_id(match(frame(S("c", "", "checkoutservice", "Convert", CONV),
                          S("s", "c", "currencyservice", "Convert", "/" + CONV))))
    assert (o["s"].outcome, o["s"].tier) == (MATCHED, "ROOT_CLIENT")


def test_weak_verb_only_tier():
    """Train Ticket: the client names only the HTTP verb."""
    o = by_id(match(frame(S("r", "", "ts-ui"), S("c", "r", "ts-ui", "", "GET"),
                          S("s", "c", "ts-auth", "", "GET /api/v1/users"))))
    assert (o["s"].outcome, o["s"].tier) == (MATCHED, "WEAK_VERB_ONLY")


def test_na_role_operation_mismatch():
    o = by_id(match(frame(S("r", ""), S("c", "r", meth="GetProduct", op=GP),
                          S("s", "c", "productcatalogservice", "GetProduct", "/Other/Thing"))))
    assert (o["s"].outcome, o["c"].outcome) == ("NA_ROLE", "NA_ROLE")


def test_na_role_caller_parent_absent_or_rejected():
    rows = frame(S("c", "gone", meth="GetProduct", op=GP),
                 S("s", "c", "productcatalogservice", "GetProduct", "/" + GP))
    o = by_id(match(rows))
    assert (o["s"].outcome, o["s"].detail) == ("NA_ROLE", "caller_parent_absent")
    o = by_id(match(rows, rejected=frame(S("gone", "", sender="attacker"))))
    assert (o["s"].outcome, o["s"].detail) == ("NA_ROLE", "caller_parent_rejected")


def test_duplicate_span_id():
    o = match(frame(S("r", ""), S("c", "r", meth="GetProduct", op=GP),
                    S("c", "r", meth="GetProduct", op=GP),
                    S("s", "c", "productcatalogservice", "GetProduct", "/" + GP)))
    assert list(o.outcome) == ["NO_WITNESS", "AMB_DUPLICATE", "AMB_DUPLICATE", "AMB_DUPLICATE"]
    assert list(o.detail)[1:] == ["dup_id", "dup_id", "partner_dup"]


def test_self_parent():
    o = by_id(match(frame(S("x", "x", meth="GetProduct", op=GP))))
    assert (o["x"].outcome, o["x"].detail) == ("AMB_SELF", "self_parent")


def test_retry_reused_context():
    """One client span, two same-method server children: a retry under one context."""
    o = by_id(match(frame(S("r", ""), S("c", "r", meth="Convert", op=CONV),
                          S("s1", "c", "currencyservice", "Convert", "/" + CONV),
                          S("s2", "c", "currencyservice", "Convert", "/" + CONV))))
    assert {o[k].outcome for k in ("c", "s1", "s2")} == {"AMB_REUSED_CTX"}


def test_fan_out_non_one_to_one():
    o = by_id(match(frame(S("r", ""), S("c", "r", meth="GetProduct", op=GP),
                          S("s1", "c", "productcatalogservice", "GetProduct", "/" + GP),
                          S("s2", "c", "currencyservice", "Convert", "/" + CONV))))
    assert {o[k].outcome for k in ("c", "s1", "s2")} == {"AMB_NON_1TO1"}


def test_missing_partner_absent_vs_rejected():
    rows = frame(S("s", "gone", "currencyservice", "Convert", "/" + CONV))
    assert by_id(match(rows))["s"].detail == "partner_absent"
    o = by_id(match(rows, rejected=frame(S("gone", "r", sender="attacker"))))
    assert (o["s"].outcome, o["s"].detail) == ("NO_WITNESS", "partner_rejected")


def test_client_whose_server_was_rejected():
    o = by_id(match(frame(S("r", ""), S("c", "r", meth="Convert", op=CONV)),
                    rejected=frame(S("s", "c", "currencyservice", sender="attacker"))))
    assert (o["c"].outcome, o["c"].detail) == ("NO_WITNESS", "partner_rejected")


def test_internal_span_and_also_client():
    o = by_id(match(frame(S("r", ""), S("i", "r"),  # same-service child, no RPC name
                          S("c", "r", meth="PlaceOrder", op="hipstershop.CheckoutService/PlaceOrder"),
                          S("m", "c", "checkoutservice", "PlaceOrder",
                            "/hipstershop.CheckoutService/PlaceOrder"),
                          S("s", "m", "currencyservice", "Convert", "/" + CONV))))
    assert (o["i"].outcome, o["i"].detail) == ("AMB_SELF", "internal")
    # m is the server of c and, by an odd shape, directly the client of s: server outcome wins
    assert (o["m"].role, o["m"].outcome, o["m"].detail) == ("server", MATCHED, "also_client")


def test_context_dropping_forgery_is_not_called_uninstrumented():
    """A forged server span that dropped its trace context (T6) becomes a root with an RPC name.
    The detail stays neutral; the operation form is only a hint."""
    o = by_id(match(frame(S("f", "", "currencyservice", "Convert", "/" + CONV))))
    assert (o["f"].outcome, o["f"].detail, o["f"].hint) == ("NO_WITNESS", "unpaired_rpc", "server_form")


# --------------------------------------------------------------- the T1 shapes
def _t1_reuse():
    """A T1 forger copies the genuine client span's id; the server span is genuine."""
    return frame(S("r", ""), S("c", "r", meth="Convert", op=CONV),
                 S("c", "r", meth="Convert", op=CONV, sender="attacker-pod"),
                 S("s", "c", "currencyservice", "Convert", "/" + CONV))


def test_t1_id_reuse_attribution_on_restores_the_genuine_pair():
    t = _t1_reuse()
    bad = attr.rejected_spans(t)
    assert list(bad) == [False, False, True, False]
    o = by_id(match(t[~bad], rejected=t[bad]))
    assert (o["s"].outcome, o["s"].partner) == (MATCHED, "c")


def test_t1_id_reuse_attribution_off_is_ambiguous():
    o = match(_t1_reuse())
    assert o.loc[3, "outcome"] == "AMB_DUPLICATE" and o.loc[3, "detail"] == "partner_dup"


def test_t1_forged_pair_matches_when_attribution_is_off():
    """P2-T6 needs this: without attribution a T1 forged caller span pairs normally, so whatever
    stops it is credited to attribution, not to the matcher."""
    t = frame(S("r", ""), S("fc", "r", meth="Convert", op=CONV, sender="attacker-pod"),
              S("fs", "fc", "currencyservice", "Convert", "/" + CONV, sender="attacker-pod"))
    assert by_id(match(t))["fs"].outcome == MATCHED
    bad = attr.rejected_spans(t)
    assert list(bad) == [False, True, True]


# --------------------------------------------------------------- properties
def _mixed():
    return pd.concat([_t1_reuse().iloc[[0, 1, 3]],
                      frame(S("c2", "r", meth="Convert", op=CONV),
                            S("x1", "c2", "currencyservice", "Convert", "/" + CONV),
                            S("x2", "c2", "currencyservice", "Convert", "/" + CONV),
                            S("y", "gone", "emailservice", "Send", "/x"),
                            S("i", "r"), S("z", "z"), S("q", "", meth="GetCart", op="h/GetCart"))],
                     ignore_index=True)


def test_every_span_has_exactly_one_outcome():
    o = match(_mixed())
    assert len(o) == len(_mixed()) and (o.outcome != "").all()
    assert o.index.equals(_mixed().index)


def test_row_order_does_not_change_outcomes():
    t = _mixed()
    a = match(t)
    for seed in range(5):
        p = t.sample(frac=1, random_state=seed)
        assert match(p).loc[a.index].equals(a)


def test_matcher_ignores_sender():
    """Same rejected set, sender garbage or absent: identical output."""
    t = _mixed()
    a = match(t)
    assert match(t.assign(sender="garbage")).equals(a)
    assert match(t.drop(columns="sender")).equals(a)
    assert "sender" not in matcher.MATCH_COLUMNS


def test_matcher_source_never_names_sender():
    tree = ast.parse(Path(matcher.__file__).read_text())
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef)) and n.body
                  and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    hits = [n for n in ast.walk(tree)
            if (isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
                and "sender" in n.value)
            or (isinstance(n, ast.Name) and "sender" in n.id)
            or (isinstance(n, ast.Attribute) and "sender" in n.attr)]
    assert hits == []


# --------------------------------------------------------------- attribution
def test_attribution_name_spaces_and_map():
    logs = pd.DataFrame({"container_name": ["frontend", "frontend"],
                         "sender": ["frontend", "frontendservice"]})
    assert list(attr.rejected_logs(logs)) == [False, True]   # never mix trace and log names
    t = frame(S("a", svc="currencyservice", sender="currencyservice-7d9f-x2"))
    assert list(attr.rejected_spans(t)) == [True]
    assert list(attr.rejected_spans(t, {"currencyservice-7d9f-x2": "currencyservice"})) == [False]


def test_missing_sender_is_a_configuration_error():
    with pytest.raises(attr.MissingSender):
        attr.rejected_spans(frame(S("a")).drop(columns="sender"))
    with pytest.raises(attr.MissingSender):
        attr.rejected_logs(pd.DataFrame({"container_name": ["x"], "sender": [""]}))


# --------------------------------------------------------------- real data
DATASET = REPO / "parity" / "dataset-rebuilt"
ANSWERS = REPO / "parity" / "tasks-rebuilt" / "re2-ob.answers.jsonl"
RE3TT = Path(os.environ.get("PROVRCA_RE3TT_ROOT", Path.home() / "g0_pilot/data/RE3/RE3-TT"))
GEN = Path(os.environ.get("PROVRCA_FRAMEBENCH_ROOT", REPO / "cases" / "framebench-dev"))
WORKERS = int(os.environ.get("PROVRCA_WORKERS", "6"))


def _re2_cases() -> dict[str, Path]:
    if not ANSWERS.exists() or not DATASET.exists():
        return {}
    dev_templates = {p.name for p in DATASET.iterdir() if p.is_dir()}
    out = {}
    for line in ANSWERS.read_text().splitlines():
        a = json.loads(line)
        template = a["source"].split("/")[0]
        assert template in dev_templates, f"{a['case_id']} is not a development case"
        out[a["source"]] = DATASET / a["source"]   # keyed by source: G0 numbered cases differently
    return out


def _re3_cases() -> dict[str, Path]:
    if not RE3TT.exists():
        return {}
    return {f"{t.name}/{r.name}": r for t in sorted(RE3TT.iterdir()) if t.is_dir()
            for r in sorted(t.iterdir()) if (r / "traces.csv").exists()}


def _census_of(path: Path) -> dict:
    return census(match(read_spans(path / "traces.csv")))


def _expected(dataset: str):
    src = {r["g0_case_id"]: r["source"]
           for r in csv.DictReader((FIX / "re2_ob_case_sources.csv").open())}
    key = (lambda c: src[c]) if dataset == "re2-ob-dev" else (lambda c: c)
    server = defaultdict(dict)
    for r in csv.DictReader((FIX / "matching_counts.csv").open()):
        if r["dataset"] == dataset:
            server[key(r["case_id"])][(r["parent_service"], r["child_service"], r["code"])] = int(r["n"])
    gaps = {key(r["case_id"]): r for r in csv.DictReader((FIX / "witness_gaps.csv").open())
            if r["dataset"] == dataset}
    unpaired = Counter()
    for r in csv.DictReader((FIX / "client_spans_without_server_child.csv").open()):
        if r["dataset"] == dataset:
            unpaired[key(r["case_id"])] += int(r["n_client_spans"]) - int(r["n_with_cross_child"])
    return server, gaps, unpaired


@pytest.fixture(scope="module", params=["re2-ob-dev", "re3-tt"])
def real(request):
    cases = _re2_cases() if request.param == "re2-ob-dev" else _re3_cases()
    if not cases:
        pytest.skip(f"no {request.param} data on this machine")
    with ProcessPoolExecutor(WORKERS) as pool:
        got = dict(zip(cases, pool.map(_census_of, cases.values())))
    return request.param, got


def test_parity_with_g0_server_side(real):
    """Like for like: G0 counted cross-service edges = the server-role rows here."""
    dataset, got = real
    server, gaps, unpaired = _expected(dataset)
    assert set(got) == set(server)
    for cid, c in got.items():
        assert c["server"] == server[cid], cid
        assert c["dangling_parent"] == int(gaps[cid]["no_witness_dangling_parent"]), cid
        assert c["unpaired_rpc"] == unpaired[cid], cid


def _overlay_cases():
    if not (GEN / "re2-ob-0001" / "clean").exists():
        return []
    return sorted(p.name for p in GEN.iterdir() if p.is_dir())


def _overlay_check(case_id: str) -> tuple[str, list[str]]:
    problems = []
    base = None
    for variant in ("clean", "FS-T2-high-20261125", "F1-T2-high-20261125", "F4-T2-high-20261125"):
        d = GEN / case_id / variant
        if not d.exists():
            continue
        t = read_spans(d / "traces.csv", extra=["sender"])
        logs = pd.read_csv(d / "logs.csv", usecols=["container_name", "sender"], dtype=str,
                           keep_default_na=False)
        if attr.rejected_spans(t).any() or attr.rejected_logs(logs).any():
            problems.append(f"{variant}: attribution rejected a T2 record")
        c = census(match(t))
        base = base or c["server"]
        if c["server"] != base:
            problems.append(f"{variant}: matching changed")
    return case_id, problems


@pytest.mark.skipif(not _overlay_cases(), reason=f"no FrameBench development set under {GEN}")
def test_t2_overlays_change_neither_attribution_nor_matching():
    """Under T2 the target sends its own forgeries, so attribution passes them, and the edited
    server spans stay matched to their genuine callers -- R1/R2 will see them."""
    with ProcessPoolExecutor(WORKERS) as pool:
        results = dict(pool.map(_overlay_check, _overlay_cases()))
    assert {k: v for k, v in results.items() if v} == {}
