"""The target rule (proposal section 12, build-guide P2 item 1), with check (a) read as a rank (K21).

A target is a service in the task's candidate list that is not the root cause, lies within two
call-graph hops of it -- a **callee** of the root cause, or a **sibling** sharing a caller with it --
and passes four checks on the clean case:

  (a) no BARO-level alarm on its own metrics: it is **not BARO's top-ranked service**. An absolute
      robust-z cut empties the development set (G0 step 8: 345 of 360 candidates exceed z = 3), so
      the G0 report fixed the rank reading. ``ALARM_TOP_K = 1`` is the cut the G0 report and
      proposal section 06 quote (3.39 eligible targets per case);
  (b) no rise in its server-span error rate;
  (c) not an upstream caller of the root cause, since callers inherit symptoms -- **any ancestor**,
      not only a direct caller (decisions log 28 Nov: every ancestor inherits symptoms; on the
      development cases this changes nothing, on RE3-TT it removes 16 of 62 targets and empties no
      case). The field keeps its name, ``c_not_upstream_caller``;
  (d) server spans after the fault and log lines, so an overlay has something to imitate.

BARO's score is RCAEval's RobustScorer: fit on the pre-fault window, take the largest |z| after it,
per column, maximised over the service's own columns of ``simple_metrics.csv`` (not the raw
cumulative counters, G0 step 8). RobustScaler is (x - median) / IQR, computed here directly rather
than through scikit-learn, including its rule that a near-zero scale becomes 1. A column that is flat before the fault has a zero IQR and is skipped,
as step 8 does.

Every candidate's checks are returned, and the draw is recorded with them, so the sealed labels can
say exactly why a target was eligible (P2-T3).
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from provrca_bench.framebench.case import CleanCase, log_name, trace_name

ALARM_TOP_K = 1


def baro_scores(case: CleanCase) -> dict[str, float]:
    """Largest post-fault robust |z| per service (trace names), over its simple-metric columns."""
    m = pd.read_csv(case.case_dir / "simple_metrics.csv")
    pre, post = m[m.time < case.inject_time], m[m.time >= case.inject_time]
    out: dict[str, float] = {}
    for col in m.columns:
        if col == "time" or "_" not in col:
            continue
        svc = trace_name(col.split("_", 1)[0])
        a = pd.to_numeric(pre[col], errors="coerce").dropna().to_numpy()
        b = pd.to_numeric(post[col], errors="coerce").dropna().to_numpy()
        if len(a) < 5 or len(b) < 1:
            continue
        q25, q50, q75 = np.percentile(a, [25, 50, 75])
        if q75 - q25 == 0:
            continue
        # scikit-learn's RobustScaler (which RCAEval's BARO uses) replaces a scale below
        # 10 * machine epsilon with 1 (_handle_zeros_in_scale). Without the same rule a column
        # whose IQR is float noise scores z ~ 1e14 instead of the |x - median| BARO reports.
        scale = q75 - q25
        if scale < 10 * np.finfo(float).eps:
            scale = 1.0
        z = np.abs((b - q50) / scale)
        z = z[np.isfinite(z)]
        if len(z):
            out[svc] = max(out.get(svc, 0.0), float(z.max()))
    return out


@dataclass(frozen=True)
class CandidateCheck:
    service: str
    shape: str                      # root | callee | sibling | other
    baro_z: float | None
    baro_rank: int | None           # 1 = BARO's top service in this case
    a_no_alarm: bool
    b_no_error_rise: bool
    c_not_upstream_caller: bool
    d_has_spans_and_logs: bool
    eligible: bool


def check_candidates(case: CleanCase) -> list[CandidateCheck]:
    scores = baro_scores(case)
    ranked = sorted(scores, key=lambda s: (-scores[s], s))
    rank = {s: i + 1 for i, s in enumerate(ranked)}
    root = case.root_cause
    out = []
    for svc in case.candidates:
        shape = ("root" if svc == root
                 else "callee" if svc in case.callees_of(root)
                 else "sibling" if case.callers_of(root) & case.callers_of(svc)
                 else "other")
        pre, post = case.server_spans(svc, post=False), case.server_spans(svc, post=True)
        err = lambda d: float((d.statusCode.fillna(0) != 0).mean()) if len(d) else 0.0
        a = rank.get(svc, 10**6) > ALARM_TOP_K
        b = not (err(post) > err(pre))
        c = svc not in case.ancestors_of(root)
        d = len(post) > 0 and log_name(svc) in case.log_containers
        out.append(CandidateCheck(
            service=svc, shape=shape, baro_z=round(scores[svc], 3) if svc in scores else None,
            baro_rank=rank.get(svc), a_no_alarm=a, b_no_error_rise=b, c_not_upstream_caller=c,
            d_has_spans_and_logs=d,
            eligible=shape in ("callee", "sibling") and a and b and c and d))
    return out


def seed_for(*parts: object) -> int:
    """hash(inputs) -> seed: the same inputs always give the same draw (P2-T4)."""
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big")


def draw_target(case: CleanCase, seed: int) -> tuple[str | None, list[CandidateCheck]]:
    """One seeded draw among the eligible candidates; None if the case has none."""
    checks = check_candidates(case)
    pool = sorted(c.service for c in checks if c.eligible)
    if not pool:
        return None, checks
    rng = np.random.default_rng(seed_for("target", case.case_id, seed))
    return pool[int(rng.integers(len(pool)))], checks


def checks_as_rows(case: CleanCase, checks: list[CandidateCheck], drawn: str | None) -> list[dict]:
    return [{"case_id": case.case_id, **asdict(c), "drawn": c.service == drawn} for c in checks]


# --------------------------------------------------------------------------------------------
# The distant-target subset (proposal section 12: "A distant-target subset (10%) is reported as a
# sensitivity check"). Step reviewed first (RESCOPE, 27 Nov): distance is measured on the full call
# graph (``CleanCase.call_edges``), checks (a)-(d) are exactly the main rule's, so the subset differs
# from the main targets in distance only, and ancestors of the root cause are excluded ((c)'s logic,
# extended: every ancestor inherits symptoms). A service absent from the graph is ``unknown`` and
# never eligible. The selection rule is frozen; eligible counts on test strata wait for G3.
# --------------------------------------------------------------------------------------------

DISTANT_MIN_HOPS = 3
DISTANT_SHARE = 0.10


def distance_classes(case: CleanCase) -> dict[str, tuple[str, int | None]]:
    """Each candidate -> (class, undirected hops from the root cause in the full call graph).
    Classes: root, neighbour (callee or sibling: the main rule's shapes), ancestor (any directed
    ancestor), near_other (2 hops, none of those), distant (>= DISTANT_MIN_HOPS), unknown (not in
    the graph, or not connected to the root cause)."""
    edges, _ = case.call_edges
    root = case.root_cause
    adj: dict[str, set[str]] = {}
    parents: dict[str, set[str]] = {}
    for a, b in edges:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
        parents.setdefault(b, set()).add(a)
    hops = {root: 0}
    frontier = [root]
    while frontier:
        nxt = []
        for u in frontier:
            for v in adj.get(u, ()):
                if v not in hops:
                    hops[v] = hops[u] + 1
                    nxt.append(v)
        frontier = nxt
    anc, stack = set(), list(parents.get(root, ()))
    while stack:
        u = stack.pop()
        if u not in anc:
            anc.add(u)
            stack.extend(parents.get(u, ()))
    callees = {b for a, b in edges if a == root}
    callers = {a for a, b in edges if b == root}
    sibs = {b for a, b in edges if a in callers} - {root}
    out = {}
    for svc in case.candidates:
        h = hops.get(svc)
        if svc == root:
            cls = "root"
        elif h is None:
            cls = "unknown"
        elif svc in anc:
            cls = "ancestor"
        elif svc in callees or svc in sibs:
            cls = "neighbour"
        elif h >= DISTANT_MIN_HOPS:
            cls = "distant"
        else:
            cls = "near_other"
        out[svc] = (cls, h)
    return out


def distant_candidates(case: CleanCase) -> list[dict]:
    """Every candidate's distance class with the main rule's checks (a), (b), (d) and the ancestor
    exclusion; ``eligible`` = distant and passing all of them."""
    checks = {c.service: c for c in check_candidates(case)}
    rows = []
    for svc, (cls, h) in sorted(distance_classes(case).items()):
        c = checks[svc]
        ok = (cls == "distant" and c.a_no_alarm and c.b_no_error_rise and c.c_not_upstream_caller
              and c.d_has_spans_and_logs)
        rows.append({"case_id": case.case_id, "service": svc, "class": cls, "hops": h,
                     "a_no_alarm": c.a_no_alarm, "b_no_error_rise": c.b_no_error_rise,
                     "c_not_upstream_caller": c.c_not_upstream_caller,
                     "d_has_spans_and_logs": c.d_has_spans_and_logs, "eligible": ok})
    return rows


def draw_distant_target(case: CleanCase, seed: int) -> tuple[str | None, list[dict]]:
    """One seeded draw among the eligible distant candidates; None if there is none."""
    rows = distant_candidates(case)
    pool = sorted(r["service"] for r in rows if r["eligible"])
    if not pool:
        return None, rows
    rng = np.random.default_rng(seed_for("distant-target", case.case_id, seed))
    return pool[int(rng.integers(len(pool)))], rows


def distant_subset(eligible_case_ids: list[str], seed: int, share: float = DISTANT_SHARE) -> list[str]:
    """The cases of one stratum that get a distant-target variant: a seeded ``share`` of the cases
    with at least one eligible distant candidate, rounded up. Call once per stratum. Each selected
    case is run beside its own neighbour target (paired design), so the reported quantity is the
    within-case difference in target blame rate, per stratum, descriptively."""
    ids = sorted(eligible_case_ids)
    if not ids:
        return []
    k = int(np.ceil(share * len(ids)))
    rng = np.random.default_rng(seed_for("distant-subset", seed, len(ids)))
    return sorted(ids[i] for i in rng.choice(len(ids), size=k, replace=False))
