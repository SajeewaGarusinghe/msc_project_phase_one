"""The rule-aware constructions: F3c (adaptive, T2) and the T1 variants of F2 and F3 (spoofer).

Both need the clean case's matched pairs (``clean_pairs``, from the P3 chain run on the **clean**
case with attribution off), so they live here rather than in ``build.py``.

**F3c** (proposal §12: "forged durations stay under the matched caller's wait"). The attacker is an
*oracle*: it is assumed to know each caller's wait and the applicability table. A real emitter sees
at most the gRPC deadline, so F3c is a worst case. Where R1 does not apply to a span's clean pair,
the span gets F3's full factor (``escape = rule_not_applicable``). Elsewhere the forged duration is
``min(dur * factor, B - g)``, where ``B = client_end - server_start`` keeps the server's end inside the
client's interval and ``g`` is drawn from the same route's clean end-gap distribution in the same
case. A zero gap would mark every capped span, because the tightest clean pair leaves 1,099 µs.
A span the cap would not lengthen is not edited and not labelled.

**T1** (proposal §05: a spoofer writes records under any service name, but cannot edit or delete
the genuine target's records). It **adds** forged attempts. Each is a forged server span claiming
the target and a forged client span claiming the genuine caller, which confirms it. Both are sent
from ``attacker-pod``.
- The client copies the genuine client's parent structure. On a root-client route it is itself a
  root, with a fresh trace id, so the pair keeps its ROOT_CLIENT tier.
- The client sits inside its parent's interval. Where the genuine parent is too short (an
  inflated F3 call cannot fit inside the real request), the spoofer forges a fresh parent of the
  same kind, as a root, around it. Forged parents are witnesses, labelled as such. A parentless
  frontend request is normal, but a parentless checkout PlaceOrder is a tell for K22.
- Start times carry jitter, and the start and end gaps are drawn from the route's clean pairs.
- F2 adds ``k = n*s/(1-s)`` attempts with code 13 on both sides. This doubles the target's request
  volume at high dose, a named limitation next to A1.
- F3 adds the smallest ``k`` that brings the target's post-fault p95 to the T2 F3 goal. Each forged
  duration is a genuine post-fault duration at or above the p95 times the factor: the values vary,
  and every one lands at or above the goal.

The §06 writer table has T1 *change* D's spans; a spoofer cannot do that, and the departure is
recorded in the decisions log.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from provrca_bench.framebench.case import CleanCase
from provrca_bench.provrca.chain import judge_spans
from provrca_bench.provrca.rules import Config, apply_rules

ATTACKER = "attacker-pod"
MIN_ROUTE_PAIRS = 20
_APPLIED = {"OK_ENCLOSED", "R1_ENCLOSURE_FAIL", "OK_STATUS", "R2_STATUS_CONFLICT"}


@lru_cache(maxsize=2)
def _clean_frame(case_dir: str) -> pd.DataFrame:
    return pd.read_csv(Path(case_dir) / "traces.csv", dtype=str, keep_default_na=False,
                       low_memory=False)


@lru_cache(maxsize=2)
def clean_pairs(case_dir: str) -> pd.DataFrame:
    """Matched clean pairs, indexed by the server spanID: route, tier, each rule's applicability,
    both sides' start and duration in µs, the client's parent and trace, and the parent's
    interval."""
    tr = _clean_frame(case_dir)
    j = judge_spans(tr, Config.load(), attribution=False)
    s = j[(j.role == "server") & (j.route != "")]
    sid = tr.spanID
    row_of = pd.Series(np.arange(len(tr)), index=sid.values)
    row_of = row_of[~row_of.index.duplicated(keep=False)]
    srv_rows = s.index.to_numpy()
    cli_rows = row_of.reindex(s.partner.values).to_numpy()
    start = tr.startTime.astype("int64").to_numpy()
    dur = tr.duration.astype("int64").to_numpy()
    par = tr.parentSpanID.to_numpy()
    p_rows = row_of.reindex(par[cli_rows]).to_numpy()
    has_p = ~np.isnan(p_rows)
    p_idx = np.where(has_p, p_rows, 0).astype(int)
    out = pd.DataFrame({
        "client": s.partner.values, "route": s.route.values, "tier": s.tier.values,
        "r1": s.r1_reason.isin(_APPLIED).values, "r2": s.r2_reason.isin(_APPLIED).values,
        "s_row": srv_rows, "c_row": cli_rows,
        "s_start": start[srv_rows], "s_dur": dur[srv_rows],
        "c_start": start[cli_rows], "c_dur": dur[cli_rows],
        "c_parent": par[cli_rows], "c_trace": tr.traceID.to_numpy()[cli_rows],
        "p_row": np.where(has_p, p_idx, -1),
        "p_start": np.where(has_p, start[p_idx], -1), "p_end": np.where(has_p, start[p_idx] + dur[p_idx], -1),
    }, index=sid.to_numpy()[srv_rows])
    out["start_gap"] = out.s_start - out.c_start
    out["end_gap"] = (out.c_start + out.c_dur) - (out.s_start + out.s_dur)
    return out


@lru_cache(maxsize=8)
def _gap_table(case_dir: str, col: str) -> dict[str, tuple[np.ndarray, str]]:
    pairs = clean_pairs(case_dir)
    pooled = pairs[col].to_numpy()
    out = {}
    for route, g in pairs.groupby("route")[col]:
        arr = g.to_numpy()
        out[route] = (arr, "case_route") if len(arr) >= MIN_ROUTE_PAIRS else (pooled, "case_all_routes")
    return out


def _gaps(case_dir: str, route: str, col: str) -> tuple[np.ndarray, str]:
    return _gap_table(case_dir, col)[route]


# ------------------------------------------------------------------ F3c
@dataclass
class F3cResult:
    edits: dict[str, dict] = field(default_factory=dict)
    escape: dict[str, str] = field(default_factory=dict)
    gap_source: set[str] = field(default_factory=set)


def f3c_edits(case: CleanCase, target: str, factor: float, rng: np.random.Generator) -> F3cResult:
    res = F3cResult()
    if factor <= 1:
        return res
    cd = str(case.case_dir)
    pairs = clean_pairs(cd)
    lo, hi = case.inject_time * 1_000_000, case.window[1] * 1_000_000
    s = case.server_spans(target)
    s = s[(s.startTime >= lo) & (s.startTime < hi)]
    sub = pairs.reindex(s.spanID.to_numpy())
    for sid, dur, r1, route, cs, cdur, ss in zip(
            s.spanID, s.duration.astype("int64"), sub.r1.to_numpy(), sub.route.to_numpy(),
            sub.c_start.to_numpy(), sub.c_dur.to_numpy(), sub.s_start.to_numpy()):
        full = int(round(dur * factor))
        if not (r1 is True or r1 == 1.0):
            res.edits[sid], res.escape[sid] = {"duration": full}, "rule_not_applicable"
            continue
        gaps, src = _gaps(cd, route, "end_gap")
        res.gap_source.add(src)
        g = int(gaps[int(rng.integers(len(gaps)))])
        bound = int(cs + cdur - ss) - max(g, 0)
        new = min(full, bound)
        if new > dur:
            res.edits[sid], res.escape[sid] = {"duration": new}, "R1_cap"
    return res


# ------------------------------------------------------------------ T1
@dataclass
class T1Result:
    lines: dict[int, list[str]] = field(default_factory=dict)     # after clean row i -> raw lines
    server_ids: list[str] = field(default_factory=list)
    client_ids: list[str] = field(default_factory=list)
    parent_ids: list[str] = field(default_factory=list)          # forged parents of forged clients
    applicable: dict[str, bool] = field(default_factory=dict)       # forged server -> its rule applies
    requested: int = 0
    skipped_no_fit: int = 0


def _hex(rng: np.random.Generator, n: int) -> str:
    return "".join(f"{int(x):02x}" for x in rng.integers(0, 256, size=n // 2))


def _p95(x: np.ndarray) -> float:
    return float(np.percentile(x, 95)) if len(x) else 0.0


def t1_inserts(case: CleanCase, target: str, family: str, dose, rng: np.random.Generator) -> T1Result:
    """Forged (client, server) pairs for T1-F2 or T1-F3, as raw CSV lines to insert after the
    base server span's line."""
    cd = str(case.case_dir)
    tr = _clean_frame(cd)
    pairs = clean_pairs(cd)
    header = list(tr.columns)
    values = tr.to_numpy()
    lo, hi = case.inject_time * 1_000_000, case.window[1] * 1_000_000
    post = case.server_spans(target)
    post = post[(post.startTime >= lo) & (post.startTime < hi)]
    n = len(post)
    bases = [b for b in post.spanID if b in pairs.index]
    res = T1Result()
    if not bases or n == 0:
        return res
    order = [bases[i] for i in rng.permutation(len(bases))]
    if family == "F2":
        s = dose.f2_share
        k = int(round(n * s / (1 - s)))
    else:
        if dose.f3_factor <= 1:
            return res
        genuine = post.duration.to_numpy(dtype=float)
        goal = _p95(genuine) * dose.f3_factor
        # bases at or above the genuine p95, so every forged duration (base x factor) lands at or
        # above the goal: varied values, and the smallest k (about 5% of n) that moves the p95
        top = set(post.spanID[post.duration.to_numpy(dtype=float) >= _p95(genuine)])
        order = [b for b in order if b in top] or order
        cand = (pairs.s_dur.reindex(order * 3).to_numpy(dtype=float) * dose.f3_factor)[: 3 * n]
        k = len(cand)
        lo_k, hi_k = 0, len(cand)
        while lo_k < hi_k:                       # smallest prefix whose union reaches the goal
            mid = (lo_k + hi_k) // 2
            if _p95(np.concatenate([genuine, cand[:mid]])) >= goal:
                hi_k = mid
            else:
                lo_k = mid + 1
        k = lo_k
    res.requested = k
    cfg = Config.load()
    forged_pairs = []
    i = 0
    seq = order * (k // max(len(order), 1) + 2)
    P = pairs.reindex(seq)
    cols = {c: P[c].to_numpy() for c in P.columns}
    while len(res.server_ids) < k and i < len(seq):
        j = i
        i += 1
        p = pd.Series({c: cols[c][j] for c in cols})
        s_row = list(values[int(p.s_row)])
        c_row = list(values[int(p.c_row)])
        s_dur = int(p.s_dur) if family == "F2" else int(round(p.s_dur * dose.f3_factor))
        sg, _ = _gaps(cd, p.route, "start_gap")
        eg, _ = _gaps(cd, p.route, "end_gap")
        gs = max(int(sg[int(rng.integers(len(sg)))]), 1)
        ge = max(int(eg[int(rng.integers(len(eg)))]), 1)
        s_start = int(p.s_start) + int(rng.integers(1, 1000))
        c_start, c_dur = s_start - gs, gs + s_dur + ge
        rooted = p.c_parent == ""
        if c_start < lo or s_start >= hi:                 # stay inside [fault, window end) (P2-T2)
            res.skipped_no_fit += 1
            continue
        fits = rooted or (p.p_start <= c_start and c_start + c_dur <= p.p_end)
        parent = None
        if not fits:
            # the genuine parent is too short to contain the forged call: forge a fresh parent of
            # the same kind (a copy of the genuine parent, as a root, long enough), as a spoofer can
            g1, g2 = int(rng.integers(100, 3000)), int(rng.integers(100, 3000))
            if c_start - g1 < lo:
                res.skipped_no_fit += 1
                continue
            parent = dict(zip(header, list(values[int(p.p_row)])))
            parent.update(spanID=_hex(rng, 16), parentSpanID="", startTime=str(c_start - g1),
                          startTimeMillis=str((c_start - g1) // 1000),
                          duration=str(g1 + c_dur + g2))
        sid, cid = _hex(rng, 16), _hex(rng, 16)
        trace = _hex(rng, 32) if (rooted or parent) else p.c_trace
        code = f"{float(dose.f2_code)}" if family == "F2" else None
        S, C = dict(zip(header, s_row)), dict(zip(header, c_row))
        S.update(spanID=sid, parentSpanID=cid, traceID=trace, startTime=str(s_start),
                 startTimeMillis=str(s_start // 1000), duration=str(s_dur))
        C.update(spanID=cid, traceID=trace, startTime=str(c_start),
                 startTimeMillis=str(c_start // 1000), duration=str(c_dur))
        if code:
            S["statusCode"], C["statusCode"] = code, code
        if parent:
            parent["traceID"] = trace
            C["parentSpanID"] = parent["spanID"]
            res.parent_ids.append(parent["spanID"])
        lines = []
        for rec in ([parent] if parent else []) + [C, S]:
            buf = io.StringIO()
            csv.writer(buf, lineterminator="").writerow([rec[h] for h in header])
            lines.append(f"{buf.getvalue()},{ATTACKER}\n")
        res.lines.setdefault(int(p.s_row), []).extend(lines)
        res.server_ids.append(sid)
        res.client_ids.append(cid)
        caller, rest = p.route.split(">")
        callee, method = rest.split("/", 1)
        forged_pairs.append({"sid": sid, "caller": caller, "callee": callee, "method": method,
                             "tier": p.tier, "server_us": s_dur, "client_us": c_dur,
                             "server_code": S["statusCode"], "client_code": C["statusCode"]})
    if forged_pairs:
        fp = pd.DataFrame(forged_pairs)
        rr = apply_rules(fp, cfg)
        rule = "r2_reason" if family == "F2" else "r1_reason"
        res.applicable = dict(zip(fp.sid, rr[rule].isin(_APPLIED)))
    return res
