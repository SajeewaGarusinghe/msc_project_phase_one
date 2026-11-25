"""P3 step 2: the applicability table, R1, R2 and the chain (build guide P3 items 3-5).

P3-T1 (rule unit tests) and P3-T2 (zero clean false contradictions) live here, with the
invariants the step-2 review asked for. The attack preview on FrameBench overlays is a separate
script, run only after the table was committed and hashed.
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from provrca_bench.provrca.chain import (applicability_in_clean, judge_logs, judge_spans,
                                         read_traces)
from provrca_bench.provrca.rules import DEFAULT_CONFIG, Config
from provrca_bench.verdicts import DEMOTED, Verdict

CFG = Config.load()
FE, CO, CUR, PC = "frontendservice", "checkoutservice", "currencyservice", "productcatalogservice"


def sp(sid, parent, svc, meth="", op="", dur="100", code="0", sender=None):
    return {"spanID": sid, "parentSpanID": parent, "serviceName": svc, "methodName": meth,
            "operationName": op, "duration": str(dur), "statusCode": str(code),
            "sender": sender or svc}


def pair(cd=1000, sd=100, cc="0", sc="0", caller=FE, callee=CUR, meth="Convert", root_client=False):
    op = f"hipstershop.X/{meth}"
    rows = [] if root_client else [sp("r", "", caller, dur=5000)]
    rows += [sp("c", "" if root_client else "r", caller, meth, op, cd, cc),
             sp("s", "c", callee, meth, "/" + op, sd, sc)]
    return pd.DataFrame(rows)


def judged(df, **kw):
    j = judge_spans(df, CFG, **kw)
    return {sid: r for sid, r in zip(df.spanID, j.itertuples())}


# --------------------------------------------------------------- the table
def test_table_is_eleven_method_routes_from_g0():
    raw = json.loads(DEFAULT_CONFIG.read_text())
    assert len(CFG.routes) == 11 and CFG.epsilon_us == 0
    assert CFG.routes[(CO, CUR, "Convert")].allowed_tiers == ("ROOT_CLIENT",)
    assert all(r.allowed_tiers == ("STRONG",) for k, r in CFG.routes.items() if k != (CO, CUR, "Convert"))
    assert all(r["proxy"] and r["source"] for r in raw["routes"])
    assert len(CFG.sha256) == 64


def test_table_rejects_duplicates_and_forbidden_tiers(tmp_path):
    raw = json.loads(DEFAULT_CONFIG.read_text())
    dup = dict(raw, routes=raw["routes"] + raw["routes"][:1])
    (tmp_path / "a.json").write_text(json.dumps(dup))
    with pytest.raises(ValueError, match="twice"):
        Config.load(tmp_path / "a.json")
    bad = json.loads(json.dumps(raw))
    bad["routes"][0]["allowed_tiers"] = ["WEAK_VERB_ONLY"]
    (tmp_path / "b.json").write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="forbids"):
        Config.load(tmp_path / "b.json")


def test_removing_a_route_makes_its_pairs_unknown(tmp_path):
    raw = json.loads(DEFAULT_CONFIG.read_text())
    raw["routes"] = [r for r in raw["routes"] if not (r["caller"] == FE and r["method"] == "Convert")]
    (tmp_path / "c.json").write_text(json.dumps(raw))
    j = judge_spans(pair(cd=10, sd=500), Config.load(tmp_path / "c.json"))
    s = j.iloc[-1]
    assert (s.r1_reason, s.r1_detail, s.r2_reason, s.r2_detail) == \
        ("NA_BOUNDARY", "route_not_listed", "NA_STATUS_LOSSY", "route_not_listed")


# --------------------------------------------------------------- P3-T1: R1
def test_r1_contradicts_a_server_longer_than_its_client():
    o = judged(pair(cd=1000, sd=1001))
    assert (o["s"].reason, o["s"].verdict, o["s"].r1_reason) == \
        ("R1_ENCLOSURE_FAIL", "contradicted", "R1_ENCLOSURE_FAIL")
    assert (o["c"].reason, o["c"].detail) == ("NO_WITNESS", "witness_side")


def test_r1_equal_durations_are_enclosed_at_epsilon_zero():
    assert judged(pair(cd=1000, sd=1000))["s"].reason == "OK_ENCLOSED"


@pytest.mark.parametrize("cc,detail", [("4", "client_deadline"), ("1", "client_cancelled")])
def test_cancelled_or_deadline_client_is_out_of_both_rules(cc, detail):
    s = judged(pair(cd=10, sd=500, cc=cc, sc="0"))["s"]
    assert (s.r1_reason, s.r1_detail, s.r2_reason, s.r2_detail) == \
        ("NA_BOUNDARY", detail, "NA_STATUS_LOSSY", detail)
    assert s.verdict == "unknown"


# --------------------------------------------------------------- P3-T1: R2
@pytest.mark.parametrize("sc,cc,r2,detail", [
    ("13", "0", "R2_STATUS_CONFLICT", ""),                 # the F2 shape
    ("0", "13", "NA_STATUS_LOSSY", "client_generated_possible"),   # G0's RST_STREAM pairs
    ("0", "0", "OK_STATUS", ""),
    ("13", "13", "OK_STATUS", ""),                         # G0's 101 genuine INTERNAL pairs
    ("13", "14", "NA_STATUS_LOSSY", "codes_differ_both_error"),
    ("13", "4", "NA_STATUS_LOSSY", "client_deadline"),     # client-generated row comes first
    ("", "0", "OK_STATUS", ""),                            # null is OK (K19)
    ("404", "0", "NA_STATUS_LOSSY", "code_out_of_range"),  # not a gRPC code
])
def test_r2_table(sc, cc, r2, detail):
    s = judged(pair(sc=sc, cc=cc))["s"]
    assert (s.r2_reason, s.r2_detail) == (r2, detail)


def test_combined_reason_prefers_a_contradiction_and_keeps_both():
    s = judged(pair(cd=1000, sd=10, sc="13", cc="0"))["s"]
    assert (s.reason, s.r1_reason, s.r2_reason) == ("R2_STATUS_CONFLICT", "OK_ENCLOSED",
                                                     "R2_STATUS_CONFLICT")
    s = judged(pair(cd=10, sd=1000, sc="13", cc="0"))["s"]
    assert s.reason == "R1_ENCLOSURE_FAIL"
    s = judged(pair(cd=1000, sd=10, sc="0", cc="13"))["s"]
    assert (s.reason, s.verdict) == ("OK_ENCLOSED", "compatible")


@pytest.mark.parametrize("sc,cc", [("0", "404"), ("0", "0"), ("500", "500")])
def test_http_routes_are_never_judged(sc, cc):
    """Both HTTP 4xx asymmetry shapes (server 0 / client 4xx) and ordinary HTTP pairs are out of
    reach on Track A: no Train Ticket route is listed (G0 steps 5-6; this supersedes build guide
    P3 item 5's "compatible")."""
    df = pd.DataFrame([sp("r", "", "ts-ui"), sp("c", "r", "ts-ui", "", "GET", 10, cc),
                       sp("s", "c", "ts-auth", "", "GET /api/v1/users", 500, sc)])
    s = judged(df)["s"]
    assert s.verdict == "unknown" and s.reason == "NA_BOUNDARY"
    assert (s.r1_detail, s.r2_reason, s.r2_detail) == ("route_not_listed", "NA_STATUS_LOSSY",
                                                       "route_not_listed")


def test_unlisted_method_on_a_listed_service_pair():
    s = judged(pair(meth="SearchProducts", callee=PC, cd=10, sd=500, sc="13"))["s"]
    assert (s.reason, s.detail) == ("NA_BOUNDARY", "route_not_listed")


def test_tier_must_be_allowed_for_the_route():
    # checkout -> currency is allowed only as ROOT_CLIENT; a STRONG pair there is out of scope
    s = judged(pair(caller=CO, callee=CUR, cd=10, sd=500))["s"]
    assert (s.reason, s.detail) == ("NA_ROLE", "tier_not_allowed")
    s = judged(pair(caller=CO, callee=CUR, cd=10, sd=500, root_client=True))["s"]
    assert s.reason == "R1_ENCLOSURE_FAIL" and s.tier == "ROOT_CLIENT"
    s = judged(pair(cd=10, sd=500, root_client=True))["s"]   # frontend -> currency as ROOT_CLIENT
    assert (s.reason, s.detail) == ("NA_ROLE", "tier_not_allowed")


def test_envoy_retry_shape_is_ambiguous_not_a_conflict():
    """Server 14 then server 0 under one client 0 (a proxy retry on UNAVAILABLE): F2's
    contradiction is sound only because the matcher refuses this join."""
    op = "hipstershop.X/Convert"
    df = pd.DataFrame([sp("r", "", FE), sp("c", "r", FE, "Convert", op, 1000, "0"),
                       sp("s1", "c", CUR, "Convert", "/" + op, 10, "14"),
                       sp("s2", "c", CUR, "Convert", "/" + op, 10, "0")])
    o = judged(df)
    assert {o[k].reason for k in ("c", "s1", "s2")} == {"AMB_REUSED_CTX"}


def test_lying_caller_gets_a_genuine_callee_contradicted():
    """Documents an accepted residual risk (§05 'at least one honest witness'): a compromised
    caller shortens its own client span, and the genuine callee's server span is contradicted."""
    s = judged(pair(cd=50, sd=100))["s"]
    assert s.verdict == "contradicted"


# --------------------------------------------------------------- the T1 ablation
def test_t1_forged_pair_passes_the_rules_without_attribution():
    df = pair(cd=1000, sd=900)
    df.loc[df.spanID.isin(["c", "s"]), "sender"] = "attacker-pod"
    assert judged(df, attribution=False)["s"].verdict == "compatible"
    o = judged(df)
    assert o["s"].reason == o["c"].reason == "ATTR_MISMATCH"


# --------------------------------------------------------------- invariants
def _random_case(seed, n=60):
    rng = np.random.default_rng(seed)
    rows = [sp("r", "", FE, dur=10_000)]
    for i in range(n):
        meth = rng.choice(["Convert", "GetProduct", "ListProducts", "SearchProducts"])
        callee = CUR if meth == "Convert" else PC
        cd, sd = int(rng.integers(1, 2000)), int(rng.integers(1, 2000))
        rows += [sp(f"c{i}", "r", FE, meth, f"x/{meth}", cd, rng.choice(["0", "0", "4", "13"])),
                 sp(f"s{i}", f"c{i}", callee, meth, f"/x/{meth}", sd, rng.choice(["0", "0", "13", "14"]))]
    return pd.DataFrame(rows)


@pytest.mark.parametrize("seed", range(5))
def test_invariants(seed):
    df = _random_case(seed)
    j = judge_spans(df, CFG)
    assert len(j) == len(df) and j.index.equals(df.index) and (j.reason != "").all()
    clients = j[df.spanID.str.startswith("c").values]
    assert not clients.verdict.isin([v.value for v in DEMOTED]).any()     # witnesses never demoted
    off = judge_spans(df, CFG, attribution=False)
    assert judge_spans(df.assign(sender="garbled"), CFG, attribution=False).equals(off)
    # shrinking a server span never turns a verdict into a contradiction by R1
    shrunk = df.assign(duration=np.where(df.spanID.str.startswith("s"),
                                         (df.duration.astype(int) // 2).astype(str), df.duration))
    j2 = judge_spans(shrunk, CFG)
    assert not ((j2.r1_reason == "R1_ENCLOSURE_FAIL") & (j.r1_reason != "R1_ENCLOSURE_FAIL")).any()


def test_logs_get_no_log_rule_or_attr_mismatch():
    logs = pd.DataFrame({"container_name": ["frontend", "currencyservice"],
                         "sender": ["frontend", "attacker-pod"]})
    j = judge_logs(logs)
    assert list(j.reason) == ["NO_WITNESS", "ATTR_MISMATCH"]
    assert list(j.detail) == ["no_log_rule", "claim_not_sender"]


def test_applicability_in_clean():
    df = pd.concat([pair(cd=1000, sd=10), pair(cd=10, sd=500, cc="4").assign(
        spanID=["r2", "c2", "s2"], parentSpanID=["", "r2", "c2"])], ignore_index=True)
    a = applicability_in_clean(df, CFG).set_index("spanID")
    assert a.loc["s"].tolist() == [True, True]
    assert a.loc["s2"].tolist() == [False, False]


# --------------------------------------------------------------- P3-T2: real data
REPO = Path(__file__).resolve().parents[1]
DATASET = REPO / "parity" / "dataset-rebuilt"
RE3TT = Path(os.environ.get("PROVRCA_RE3TT_ROOT", Path.home() / "g0_pilot/data/RE3/RE3-TT"))
WORKERS = int(os.environ.get("PROVRCA_WORKERS", "6"))


def _clean_dirs():
    out = []
    if DATASET.exists():
        out += sorted(p for p in DATASET.glob("*/*") if (p / "traces.csv").exists())
    if RE3TT.exists():
        out += sorted(p for p in RE3TT.glob("*/*") if (p / "traces.csv").exists())
    return out


def _clean_summary(path: Path):
    j = judge_spans(read_traces(path / "traces.csv", with_sender=False), CFG, attribution=False)
    s = j[j.role == "server"]
    return str(path), {
        "contradicted": int((j.verdict == Verdict.CONTRADICTED.value).sum()),
        "r1_applied": int(s.r1_reason.isin(["OK_ENCLOSED", "R1_ENCLOSURE_FAIL"]).sum()),
        "r2_applied": int(s.r2_reason.isin(["OK_STATUS", "R2_STATUS_CONFLICT"]).sum()),
        "server": len(s)}


@pytest.mark.skipif(not _clean_dirs(), reason="no clean development data on this machine")
def test_p3_t2_zero_clean_false_contradictions():
    with ProcessPoolExecutor(WORKERS) as pool:
        res = dict(pool.map(_clean_summary, _clean_dirs()))
    assert len(res) == 66
    bad = {k: v for k, v in res.items() if v["contradicted"]}
    assert bad == {}
    re2 = [v for k, v in res.items() if "RE3-TT" not in k]
    re3 = [v for k, v in res.items() if "RE3-TT" in k]
    # every clean RE2-OB pair on a covered route is in R1's and R2's reach; none on Train Ticket
    assert sum(v["r1_applied"] for v in re2) > 0 and sum(v["r1_applied"] for v in re3) == 0
    assert sum(v["r2_applied"] for v in re3) == 0


def test_the_table_is_locked():
    """P3 item 7: any change to the table must come with a new lock and a decisions-log entry."""
    lock = (DEFAULT_CONFIG.parent / "track-a.lock").read_text().strip()
    assert CFG.sha256 == lock
    assert json.loads(DEFAULT_CONFIG.read_text())["status"].startswith("locked")
