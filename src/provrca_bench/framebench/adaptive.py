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
the genuine target's records). It **adds** forged attempts by **replaying recorded requests** (K22
revision, 27 Nov; ``t1_inserts``): a whole post-fault trace through the target is copied with fresh
ids, placed at a time drawn from the target's own traffic, and every target server span in it is a
claim (F2: the dose code on it and its client; F3: duration x factor, ancestors lengthened and later
spans pushed by the same delta). Every replayed span is sent from ``attacker-pod``; the claims'
callers and the rest of the copy are witnesses. F2 adds ``k = n*s/(1-s)`` claims, doubling the
target's volume at high dose (a named limitation next to A1), and the replay raises the volume of
every service on the copied traces too. F3 adds traces until the target's post-fault p95 reaches
the T2 F3 goal, starting from traces that hold a span at or above the genuine p95, weighted by
1/(1 - 0.95^k) for a trace with k target server spans (round 2). A copy's first claim is placed
at a genuine trace's first target server start within +-60 s of the base, plus a genuine <= 5 ms neighbour gap
(with the case's share of such neighbours) or U(+-2 s) (round 2: density-matched, so a copy sees
the load a genuine request sees; round 1's uniform +-60 s put copies in quieter moments). The first
builder (forged pairs with no children, in 2-3-span or +2 traces) was separable by structure alone
(K22 sweep, 27 Nov).

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
    server_ids: list[str] = field(default_factory=list)          # the claims
    client_ids: list[str] = field(default_factory=list)          # their (replayed) callers
    witness_ids: list[str] = field(default_factory=list)         # every other replayed span
    applicable: dict[str, bool] = field(default_factory=dict)       # claim -> its rule applies
    requested: int = 0
    replays: int = 0
    distinct_traces: int = 0
    eligible_traces: int = 0
    skipped_no_fit: int = 0
    skipped_rc_cap: int = 0
    rc_added: int = 0
    rc_window: int = 0
    volume_ratio: dict[str, float] = field(default_factory=dict)  # per service, overlay/clean


T1_DILATION = (0.95, 1.05)       # per replay: no copy is an exact twin of a genuine trace
T1_PLACE_US = 60_000_000         # a copy lands within +-60 s of its base's own time
T1_NEAR_US = 5_000                 # round 2: "a neighbour within 5 ms"
T1_JITTER_US = 2_000_000           # round 2: otherwise U(+-2 s) about the drawn genuine start
# A root-cause volume cap (senior review change 2) is supported but OFF: at 20% it cut T1-F2-high to
# a third of its claims on recommendation cases (every such trace also calls the root cause), so the
# dose would no longer be the T2 dose. The ratio is recorded per variant instead (meta
# ``t1_volume_ratio``) and disclosed; replays that avoid the root cause are still drawn first.
T1_RC_CAP: float | None = None


def _hex(rng: np.random.Generator, n: int) -> str:
    return "".join(f"{int(x):02x}" for x in rng.integers(0, 256, size=n // 2))


def _p95(x: np.ndarray) -> float:
    return float(np.percentile(x, 95)) if len(x) else 0.0


def _hhmm_us(us: int) -> str:
    return pd.Timestamp(int(us), unit="us", tz="UTC").strftime("%H:%M")


@lru_cache(maxsize=2)
def _trace_blocks(case_dir: str):
    """Rows of each trace (file order), and the file's contiguous trace blocks: first row, last
    row and earliest start of each, for placing a replayed trace as a block of its own."""
    tr = _clean_frame(case_dir)
    rows_of = tr.groupby("traceID", sort=False).indices
    tid = tr.traceID.to_numpy()
    st = tr.startTime.astype("int64").to_numpy()
    first = np.flatnonzero(np.r_[True, tid[1:] != tid[:-1]])
    last = np.r_[first[1:] - 1, len(tid) - 1]
    bmin = np.minimum.reduceat(st, first)
    order = np.argsort(bmin, kind="stable")
    return rows_of, last[order], bmin[order]


def t1_inserts(case: CleanCase, target: str, family: str, dose, rng: np.random.Generator) -> T1Result:
    """T1-F2 / T1-F3 as **replayed requests** (K22 revision, 27 Nov). The spoofer replays a whole
    recorded post-fault trace that passes through the target, with fresh trace and span ids, at a
    time drawn from the target's genuine traffic, and changes only the claim: every target server
    span of the copy (F2: the dose code on it and on its client; F3: its duration x factor, with
    its ancestors lengthened and later spans of the trace pushed by the same delta, so the copy
    stays well nested). Every replayed span is sent from ``attacker-pod``. The copy keeps the
    genuine structure (children, trace size, parents), which the first builder did not (K22,
    27 Nov: forged server spans had no children and sat in 2-3-span or +2 traces)."""
    cd = str(case.case_dir)
    tr = _clean_frame(cd)
    pairs = clean_pairs(cd)
    pairs = pairs[~pairs.index.duplicated(keep=False)]
    header = list(tr.columns)
    values = tr.to_numpy()
    H = {h: i for i, h in enumerate(header)}
    lo, hi = case.inject_time * 1_000_000, case.window[1] * 1_000_000
    allsrv = case.server_spans(target)
    post = allsrv[(allsrv.startTime >= lo) & (allsrv.startTime < hi)]
    n = len(post)
    res = T1Result()
    if n == 0:
        return res
    tid_of = dict(zip(tr.spanID, tr.traceID))
    in_window = post.groupby(post.spanID.map(tid_of)).spanID.apply(list)
    every = allsrv.groupby(allsrv.spanID.map(tid_of)).size()
    eligible = [t for t, ids in in_window.items()
                if len(ids) == every.get(t, 0)
                and all(s in pairs.index and pairs.at[s, "c_trace"] == t for s in ids)]
    res.eligible_traces = len(eligible)
    if not eligible:
        return res
    svc = tr.serviceName.to_numpy()
    tst = tr.startTime.astype("int64").to_numpy()
    rows_of, b_last, b_min = _trace_blocks(cd)
    rc_in = {t: int((svc[rows_of[t]] == case.root_cause).sum()) for t in eligible}
    win = (tst >= lo) & (tst < hi)
    clean_vol = pd.Series(svc[win]).value_counts()
    res.rc_window = int(clean_vol.get(case.root_cause, 0))
    rc_cap = np.inf if T1_RC_CAP is None else T1_RC_CAP * res.rc_window
    perm = [eligible[i] for i in rng.permutation(len(eligible))]
    # traces that avoid the root cause first, so the replay does not boost the true signal
    order = [t for t in perm if rc_in[t] == 0] + [t for t in perm if rc_in[t] > 0]
    genuine = post.duration.to_numpy(dtype=float)
    # density-matched placement (round 2, review change 4): a copy's first claim starts at a
    # genuine target server start t_g within +-60 s of the base, plus, with probability pi (the
    # case's share of genuine target server spans with a neighbour within 5 ms), a genuine
    # nearest-neighbour gap of <= 5 ms, otherwise U(+-2 s)
    # anchors are the genuine traces' first target server starts, not every span's: a copy is a
    # trace, and drawing per span over-weights moments dense with multi-call traces (pre-flight,
    # round 2: currency burst_100ms AUC 0.57-0.64)
    g_st = np.sort(post.groupby(post.spanID.map(tid_of)).startTime.min().to_numpy(dtype=np.int64))
    _d = np.diff(g_st)
    nn = np.minimum(np.r_[np.inf, _d], np.r_[_d, np.inf])
    nn = nn[np.isfinite(nn)].astype(np.int64)
    close = nn[nn <= T1_NEAR_US]
    pi = float((nn <= T1_NEAR_US).mean()) if len(nn) else 0.0
    if family == "F2":
        s = dose.f2_share
        k = int(round(n * s / (1 - s)))
        goal = None
    else:
        if dose.f3_factor <= 1:
            return res
        goal = _p95(genuine) * dose.f3_factor
        top = set(post.spanID[genuine >= _p95(genuine)])
        pool = [t for t in order if top & set(in_window[t])]
        if pool:
            # round 2 (review, optional 1): a copy brings all k of its target server spans, so
            # an unweighted pool over-represents many-call traces; weight 1/(1 - 0.95^k), drawn
            # without replacement (Efraimidis-Spirakis), root-cause-free traces still first
            def weighted(ts):
                w = np.array([1.0 / (1.0 - 0.95 ** len(in_window[t])) for t in ts])
                key = rng.random(len(ts)) ** (1.0 / w)
                return [ts[i] for i in np.argsort(-key, kind="stable")]
            order = (weighted([t for t in pool if rc_in[t] == 0])
                     + weighted([t for t in pool if rc_in[t] > 0]))
        k = 3 * n                                      # a cap; the p95 goal stops it first
    res.requested = k if family == "F2" else 0
    added = pd.Series(dtype=float)
    blocks: list[tuple[int, int, list[str]]] = []
    forged_pairs, forged_durs = [], []
    used: set[str] = set()
    j = 0
    while len(res.server_ids) < k and j < 3 * n:
        t = order[j % len(order)]
        j += 1
        if rc_in[t] and res.rc_added + rc_in[t] > rc_cap:
            res.skipped_rc_cap += 1
            continue
        rows = rows_of[t]
        recs = [list(values[r]) for r in rows]
        sid = [str(r[H["spanID"]]) for r in recs]
        par = [str(r[H["parentSpanID"]]) for r in recs]
        st = np.array([int(r[H["startTime"]]) for r in recs], dtype=np.int64)
        du = np.array([int(r[H["duration"]]) for r in recs], dtype=np.int64)
        pos = {x: i for i, x in enumerate(sid)}
        claims = sorted((pos[c] for c in in_window[t]), key=lambda i: st[i])
        base_t = int(st[claims[0]])
        client_of = {c: pos.get(str(pairs.at[sid[c], "client"])) for c in claims}
        if family == "F3":
            def chain(i):                               # ancestors inside the trace
                out, p = [], pos.get(par[i])
                while p is not None and p not in out:
                    out.append(p)
                    p = pos.get(par[p])
                return out
            for c in claims:
                new = int(round(du[c] * dose.f3_factor))
                delta, old_end = new - int(du[c]), int(st[c] + du[c])
                anc = set(chain(c))
                desc = {i for i in range(len(sid)) if c in chain(i)}
                du[c] = new
                for a in anc:
                    du[a] += delta
                for i in range(len(sid)):
                    if i != c and i not in anc and i not in desc and st[i] >= old_end:
                        st[i] += delta
        # dilate the copy about its root's start (both ends mapped, so nesting is kept exactly)
        f = float(rng.uniform(*T1_DILATION))
        r0 = int(st.min())
        en = st + du
        st = r0 + np.round((st - r0) * f).astype(np.int64)
        du = (r0 + np.round((en - r0) * f).astype(np.int64)) - st
        shift = None
        first = int(st[claims[0]])
        a, b = np.searchsorted(g_st, [base_t - T1_PLACE_US, base_t + T1_PLACE_US])
        for _ in range(20 if b > a else 0):
            t_g = int(g_st[int(rng.integers(a, b))])
            if len(close) and rng.random() < pi:
                delta = int(close[int(rng.integers(len(close)))]) * (1 if rng.random() < 0.5 else -1)
            else:
                delta = int(rng.integers(-T1_JITTER_US, T1_JITTER_US + 1))
            d = t_g + delta - first
            if st.min() + d >= lo and st.max() + d < hi:
                shift = d
                break
        if shift is None:
            res.skipped_no_fit += 1
            continue
        st += shift
        idmap: dict[str, str] = {}

        def fresh(x: str) -> str:
            if x not in idmap:
                idmap[x] = _hex(rng, 16)
            return idmap[x]
        trace = _hex(rng, 32)
        code = f"{float(dose.f2_code)}" if family == "F2" else None
        lines = []
        for i, r in enumerate(recs):
            r = list(r)
            r[H["traceID"]], r[H["spanID"]] = trace, fresh(sid[i])
            r[H["parentSpanID"]] = fresh(par[i]) if par[i] else ""
            r[H["startTime"]], r[H["startTimeMillis"]] = str(st[i]), str(st[i] // 1000)
            r[H["duration"]], r[H["time"]] = str(du[i]), _hhmm_us(st[i])
            if code and (i in claims or i in client_of.values()):
                r[H["statusCode"]] = code
            buf = io.StringIO()
            csv.writer(buf, lineterminator="").writerow(r)
            lines.append(f"{buf.getvalue()},{ATTACKER}\n")
        root_start = int(st.min())
        b = int(np.searchsorted(b_min, root_start, side="right")) - 1
        blocks.append((int(b_last[max(b, 0)]), root_start, lines))
        res.replays += 1
        res.rc_added += rc_in[t]
        added = added.add(pd.Series(svc[rows]).value_counts(), fill_value=0)
        used.add(t)
        roles = set()
        for c in claims:
            p = pairs.loc[sid[c]]
            cl = client_of[c]
            res.server_ids.append(idmap[sid[c]])
            roles.add(c)
            if cl is not None:
                res.client_ids.append(idmap[sid[cl]])
                roles.add(cl)
            caller, rest = p.route.split(">")
            callee, method = rest.split("/", 1)
            forged_pairs.append({"sid": idmap[sid[c]], "caller": caller, "callee": callee,
                                 "method": method, "tier": p.tier, "server_us": int(du[c]),
                                 "client_us": int(du[cl]) if cl is not None else np.nan,
                                 "server_code": code or recs[c][H["statusCode"]],
                                 "client_code": (code or recs[cl][H["statusCode"]])
                                 if cl is not None else ""})
            forged_durs.append(float(du[c]))
        res.witness_ids += [idmap[sid[i]] for i in range(len(sid)) if i not in roles]
        if goal is not None and _p95(np.concatenate([genuine, forged_durs])) >= goal:
            break
    res.distinct_traces = len(used)
    res.volume_ratio = {k2: round(float((clean_vol.get(k2, 0) + v) / clean_vol.get(k2, 0)), 4)
                        for k2, v in added.items() if clean_vol.get(k2, 0)}
    for row, _, lines in sorted(blocks, key=lambda x: (x[0], x[1])):
        res.lines.setdefault(row, []).extend(lines)
    if forged_pairs:
        fp = pd.DataFrame(forged_pairs)
        rr = apply_rules(fp, Config.load())
        rule = "r2_reason" if family == "F2" else "r1_reason"
        res.applicable = dict(zip(fp.sid, rr[rule].isin(_APPLIED)))
    return res
