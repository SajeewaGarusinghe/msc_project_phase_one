"""The honest faults: AF1 (clock skew) and AF2 (service mislabelling) (proposal §06, EQ1, E3).

Both are the *everyday* form of the T2 threat (§05: "a wrong clock, a mislabelled deployment"):
nobody lies on purpose, but a record still makes a false claim under a real identity. Step 5 of
P3 builds them; the plan and its senior review are in Msc-project
``09-results/p3/2026-11-27-step5/probes/``.

**AF1, D's clock skewed by ±250 ms.** The clock model is a *node* clock: RCAEval collects spans
with the OpenTelemetry SDK (process clock) and logs with Vector/Loki from the container runtime's
log files (node clock), and a pod's process clock *is* its node's clock. So every span D writes
(server, client, internal) and every line in D's container move by the same δ, over the **whole
file** (the offset predates the incident). ``duration`` does not change: it is measured on one
clock. Metrics are scraped by Prometheus on its own clock and are untouched (K23).
*Limitation, recorded:* a real node skew moves every pod on D's node; only D is shifted
(co-located services are counted per case from ``pod-node-1.csv`` and reported).
``time`` (HH:MM) is re-rendered only when the shift crosses a minute. Log rows are **not**
re-sorted: row positions stay, so verdicts can be compared row for row with the clean case.

**AF2, some of B's requests mislabelled as D.** One of B's replicas runs with D's service name, so
the spans it writes for a request (B's server span and the B-written spans beneath it in the same
trace) claim ``serviceName = D``. The collector still attaches B's identity: ``sender = B``.
Logs are untouched (``container_name`` is platform-written). A mislabelled deployment predates the
incident, so a **constant share of B's requests over the whole file** is relabelled, pre- and
post-fault at the same rate; a post-fault-only relabel would give D an attack-shaped volume jump
at fault onset. The volume is ``min(k_D, round(s · n_B))``: ``k_D = n_D · s/(1−s)`` with F2's high
share s (the T1-F2 volume rule) caps it relative to D, so a large B cannot swamp D, and ``s · n_B``
caps it relative to B, so a small B is never emptied. One level only (E3 has no dose axis).

B is drawn (seeded) from D's clean-graph neighbours (its callers, callees, and siblings under a
common caller) that write at least ``MIN_B_SERVER`` server spans, are not the root cause, and do
not call the root cause (a relabelled B client span would leave the root cause's genuine server
spans without a witness). The draw is uniform over the eligible neighbours (review item 4: a rule
that preferred "room for k" made productcatalogservice the only choice in practice, so the draw was
forced, not seeded). ``capped_by_b`` records when B's own size set the volume.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from provrca_bench.framebench.case import CleanCase, log_name
from provrca_bench.framebench.doses import F2_SHARE

AF1_SKEW_US = 250_000
AF1_LEVELS = {"plus": +1, "minus": -1}
AF2_LEVEL = "std"
AF2_SHARE = F2_SHARE["high"]
MIN_B_SERVER = 20
#: frontendservice is the trace root (0 server spans): it can never be a B.
NEVER_B = {"frontendservice"}


def _shift_hhmm(hhmm: str, minutes: int) -> str:
    if not minutes:
        return hhmm
    h, m = (int(x) for x in str(hhmm).split(":"))
    t = (h * 60 + m + minutes) % (24 * 60)
    return f"{t // 60:02d}:{t % 60:02d}"


# ------------------------------------------------------------------ AF1
@dataclass
class AF1Result:
    delta_us: int
    span_edits: dict[str, dict] = field(default_factory=dict)
    log_edits: dict[int, dict] = field(default_factory=dict)       # row position -> new fields
    co_located: list[str] = field(default_factory=list)


def af1_edits(case: CleanCase, target: str, level: str, logs: pd.DataFrame) -> AF1Result:
    if level not in AF1_LEVELS:
        raise ValueError(f"AF1 level must be one of {sorted(AF1_LEVELS)}")
    delta = AF1_LEVELS[level] * AF1_SKEW_US
    res = AF1Result(delta_us=delta)
    tr = pd.read_csv(case.case_dir / "traces.csv", usecols=["spanID", "serviceName", "startTime", "time"],
                     dtype={"spanID": str, "time": str}, low_memory=False)
    mine = tr[tr.serviceName == target]
    for sid, st, hhmm in zip(mine.spanID, mine.startTime.astype("int64"), mine.time):
        new = int(st) + delta
        dmin = new // 60_000_000 - int(st) // 60_000_000
        res.span_edits[sid] = {"startTime": str(new), "startTimeMillis": str(new // 1000),
                               "time": _shift_hhmm(hhmm, dmin)}
    rows = np.flatnonzero((logs.container_name.astype(str) == log_name(target)).to_numpy())
    ts = logs.timestamp.astype("int64").to_numpy()
    for i in rows:
        new = int(ts[i]) + delta * 1000
        dmin = new // 60_000_000_000 - int(ts[i]) // 60_000_000_000
        res.log_edits[int(i)] = {"timestamp": new, "time": _shift_hhmm(logs.time.iat[i], dmin)}
    pn = case.case_dir / "pod-node-1.csv"
    if pn.exists():
        nodes = pd.read_csv(pn)
        svc = nodes.POD.str.rsplit("-", n=2).str[0]
        mine_nodes = set(nodes.NODE_NAME[svc == log_name(target)])
        res.co_located = sorted(set(svc[nodes.NODE_NAME.isin(mine_nodes)]) - {log_name(target)})
    return res


def af1_sign_assignment(case_ids: list[str], seed: int) -> dict[str, str]:
    """E3's one AF1 variant per case: a seeded sign, balanced within the list given (call it once
    per stratum). Pre-declared in the run manifest before any run (review change 2)."""
    ids = sorted(case_ids)
    perm = np.random.default_rng(seed).permutation(len(ids))
    half = len(ids) // 2
    return {ids[j]: ("plus" if k < half else "minus") for k, j in enumerate(perm)}


# ------------------------------------------------------------------ AF2
@dataclass
class AF2Result:
    b: str | None = None
    capped_by_b: bool = False
    candidates: dict[str, int] = field(default_factory=dict)       # eligible B -> server spans
    excluded: dict[str, str] = field(default_factory=dict)         # neighbour -> why not
    requested: int = 0
    requests: int = 0                                                # B server spans relabelled
    span_edits: dict[str, dict] = field(default_factory=dict)       # spanID -> {"serviceName": D}
    sender: dict[str, str] = field(default_factory=dict)            # spanID -> B
    pre_post: tuple[int, int] = (0, 0)                               # relabelled requests pre / post fault


def neighbours(case: CleanCase, target: str) -> set[str]:
    callers = case.callers_of(target)
    sibs = set().union(*(case.callees_of(a) for a in callers)) if callers else set()
    return (callers | case.callees_of(target) | sibs) - {target}


def af2_edits(case: CleanCase, target: str, rng: np.random.Generator) -> AF2Result:
    res = AF2Result()
    sp = case.spans
    n_d = int(((sp.serviceName == target) & sp.is_server).sum())
    k = int(round(n_d * AF2_SHARE / (1 - AF2_SHARE)))
    res.requested = k
    for b in sorted(neighbours(case, target)):
        n_b = int(((sp.serviceName == b) & sp.is_server).sum())
        if b in NEVER_B:
            res.excluded[b] = "trace_root"
        elif b == case.root_cause:
            res.excluded[b] = "root_cause"
        elif case.root_cause in case.callees_of(b):
            res.excluded[b] = "calls_root_cause"
        elif n_b < MIN_B_SERVER:
            res.excluded[b] = "too_few_server_spans"
        else:
            res.candidates[b] = n_b
    if not res.candidates or k == 0:
        return res
    pool = sorted(res.candidates)
    b = pool[int(rng.integers(len(pool)))]
    res.b = b
    srv = sp[(sp.serviceName == b) & sp.is_server]
    cap = int(round(AF2_SHARE * len(srv)))
    take = min(k, cap)
    res.capped_by_b = cap < k
    chosen = srv.iloc[np.sort(rng.choice(len(srv), size=take, replace=False))]
    res.requests = take
    post = int(chosen.post.sum())
    res.pre_post = (take - post, post)
    # B-written descendants in the same trace: walk child links while the child is B's
    mine = sp[sp.serviceName == b]
    kids: dict[str, list[str]] = {}
    for sid, par in zip(mine.spanID, mine.parentSpanID):
        kids.setdefault(str(par), []).append(str(sid))
    stack = [str(s) for s in chosen.spanID]
    seen: set[str] = set()
    while stack:
        s = stack.pop()
        if s in seen:
            continue
        seen.add(s)
        stack.extend(kids.get(s, ()))
    for s in seen:
        res.span_edits[s] = {"serviceName": target}
        res.sender[s] = b
    return res
