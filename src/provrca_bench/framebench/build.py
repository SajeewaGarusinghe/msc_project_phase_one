"""One overlay variant: (clean case, target, family, threat model, dose, seed) -> case bytes + labels.

The same inputs always give the same bytes (P2-T4): every random choice draws from a generator
seeded by a hash of the inputs, and genuine rows are carried through unchanged.

What is written
  * ``traces.csv``: every genuine line byte for byte, plus a ``sender`` column; edited spans
    (F2, F3, FS) differ only in ``duration`` and/or ``statusCode``, written in the file's own format
    (``13.0``, like the genuine non-OK spans).
  * ``logs.csv``: every genuine row plus a ``sender`` column; injected lines (F1, F4, A1) replay the
    shape of a genuine burst of the target at another genuine line's time, inserted in timestamp
    order, in the target's own level casing.
  * derived files rebuilt by ``rcaeval-convert derive``, the same call as for clean cases (A3).
    Span families change only ``tracets_*``; ``simple_metrics.csv`` and ``metrics.csv`` are platform-
    written (cAdvisor, Envoy) and are never touched (K23). Untouched files are hard-linked.

What is never written into the case: the family, dose, seed, target or any label. Those go to the
sealed directory only (A4), as ``<case>/<variant>.labels.jsonl`` and ``<case>/<variant>.meta.json``.

Threat models: **T2** (the genuine emitter: the target is the sender of its own false records,
edited in place) for every family, and **T1** (a spoofer that *adds* replayed requests from
``attacker-pod``, with the target's server spans as the claims) for F2 and F3; the rule-aware constructions are in ``adaptive.py``. F2c and FSc
(escape routes chosen by distinguishability, step 4b); AF1/AF2, the honest faults, are in
``honest.py`` (step 5).

Labels carry ``rule_applicable`` (as delivered, derived from what the generator did),
``rule_applicable_clean`` (the clean pair, from the P3 chain on the clean case) and ``escape``.
"""
from __future__ import annotations

import csv
import io
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from provrca_bench.contracts import LabelRecord
from provrca_bench.framebench import adaptive, honest
from provrca_bench.framebench.case import CleanCase, log_name
from provrca_bench.framebench.doses import CaseDose, dose_for
from provrca_bench.framebench.target import CandidateCheck, seed_for

FAMILIES = ("clean", "F1", "F2", "F3", "FS", "F4", "A1", "F3c", "F2c", "FSc", "F2cD", "AF1", "AF2")
#: the honest faults (step 5): not attacks, so no rule is targeted and they have their own levels
HONEST = {"AF1": tuple(honest.AF1_LEVELS), "AF2": (honest.AF2_LEVEL,)}
SPAN_FAMILIES = {"F2", "F3", "FS", "F3c", "F2c", "FSc", "F2cD"}
T1_FAMILIES = {"F2", "F3"}
#: the rule each span family's forgery targets (label ``rule_applicable``)
RULE_OF = {"F2": ("r2",), "F3": ("r1",), "F3c": ("r1",), "FS": ("r1", "r2"),
           "F2c": ("r2",), "FSc": ("r1", "r2"), "F2cD": ("r2",)}
# Step 4b (decisions log 26 Nov). F2c/FSc are F2/FS with the witness link cut: the forged server
# span's parentSpanID is replaced by a fresh id that is not in the file (route A, the cause of real
# sampling loss), so the matcher finds no caller attempt. F2cD is a development-only descriptive
# variant (route D): F2's spans with a status outside gRPC's 0-16, which R2 treats as unknown. It
# repeats the K22 value defect and is never used in E2.
BASE_OF = {"F2c": "F2", "FSc": "FS", "F2cD": "F2"}
ESCAPE_OF = {"F2c": "witness_unlinked", "FSc": "witness_unlinked", "F2cD": "code_range"}
F2CD_CODE = 269            # 13 + 2**8: outside 0-16, not an HTTP-looking value; recorded, not tuned
LOG_FAMILIES = {"F1", "F4", "A1"}
HARDLINK = ("metrics.csv", "simple_metrics.csv", "metrics_postprocess.log",
            "pod-node-1.csv", "pod-node-2.csv")
COPY = ("inject_time.txt",)

# The operation each service names in an error line, in the gRPC error form the deployment's own
# genuine ERROR lines use (frontend: "rpc error: code = Unavailable desc = ..."). Declarative only:
# no imperative or agent-directed wording (P2-T7).
OPERATION = {
    "adservice": "GetAds", "cartservice": "GetCart", "checkoutservice": "PlaceOrder",
    "currencyservice": "Convert", "emailservice": "SendOrderConfirmation",
    "paymentservice": "PaymentService#Charge", "productcatalogservice": "GetProduct",
    "recommendationservice": "ListRecommendations", "shippingservice": "ShipOrder",
}
GRPC_FAILURES = (("Unavailable", "connection error"),
                 ("DeadlineExceeded", "context deadline exceeded"),
                 ("Internal", "stream terminated by RST_STREAM"))
F4_WINDOW = (300, 120)        # F4 lines fall 5 to 2 minutes before the fault


def variant_name(family: str, threat: str, dose: str, seed: int) -> str:
    return "clean" if family == "clean" else f"{family}-{threat}-{dose}-{seed}"


@dataclass
class BuildResult:
    case_id: str
    variant: str
    target: str | None
    family: str
    dose: CaseDose | None
    out_dir: Path
    injected: int = 0
    edited: int = 0
    labels: list[dict] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------------------------
# Selection: which records change, and how
# --------------------------------------------------------------------------------------------

def _span_edits(case: CleanCase, target: str, family: str, dose: CaseDose,
                rng: np.random.Generator) -> dict[str, dict]:
    """spanID -> {"duration": int} and/or {"statusCode": str} (and {"parentSpanID": str} for
    F2c/FSc), on the target's post-fault server spans inside the standard window."""
    variant_of = family
    family = BASE_OF.get(family, family)
    start_us, end_us = case.inject_time * 1_000_000, case.window[1] * 1_000_000
    s = case.server_spans(target)
    s = s[(s.startTime >= start_us) & (s.startTime < end_us)]
    ids = list(s.spanID)
    dur = dict(zip(s.spanID, s.duration))
    edits: dict[str, dict] = {}
    if family == "F3":
        if dose.f3_factor > 1:
            for sid in ids:
                edits[sid] = {"duration": int(round(dur[sid] * dose.f3_factor))}
        return edits
    k = int(round(dose.f2_share * len(ids)))
    chosen = sorted(rng.choice(len(ids), size=k, replace=False)) if k else []
    for i in chosen:
        sid = ids[i]
        e = {"statusCode": f"{float(dose.f2_code)}"}
        if family == "FS" and dose.f3_factor > 1:
            e["duration"] = int(round(dur[sid] * dose.f3_factor))
        edits[sid] = e
    if variant_of == "F2cD":
        for e in edits.values():
            e["statusCode"] = f"{float(F2CD_CODE)}"
    elif variant_of in ("F2c", "FSc"):
        taken = set(case.spans.spanID.astype(str)) | set(case.spans.parentSpanID.dropna().astype(str))
        for sid in edits:
            new = _fresh_id(rng, taken)
            taken.add(new)
            edits[sid]["parentSpanID"] = new
    return edits


def _fresh_id(rng: np.random.Generator, taken: set[str]) -> str:
    while True:
        new = "".join(f"{int(x):02x}" for x in rng.integers(0, 256, size=8))
        if new not in taken:
            return new


def _level_case(levels: pd.Series) -> str:
    """'upper' if the service writes INFO, otherwise 'lower' (info)."""
    vals = levels.dropna().astype(str)
    return "upper" if len(vals) and (vals.str.isupper().mean() > 0.5) else "lower"


BURST_GAP_NS = 1_000_000_000      # a burst: one message, consecutive gaps <= 1 s (K22's dup flag)
EPISODE_GAP_NS = 10_000_000       # an episode: a container's lines with consecutive gaps <= 10 ms
MAX_PLACE_TRIES = 50


def _hhmm(ts_ns: int) -> str:
    """The ``time`` column: UTC HH:MM of the timestamp, as RCAEval writes it."""
    return pd.Timestamp(int(ts_ns), unit="ns", tz="UTC").strftime("%H:%M")


def _runs(ts: np.ndarray, key: np.ndarray | None, gap: int) -> list[np.ndarray]:
    """Positions grouped into maximal runs (of one ``key``, if given) whose consecutive times are at
    most ``gap`` apart, in time order within each run."""
    order = np.lexsort((ts, key)) if key is not None else np.argsort(ts, kind="stable")
    if not len(order):
        return []
    same = np.diff(ts[order]) <= gap
    if key is not None:
        same &= key[order][1:] == key[order][:-1]
    cuts = np.flatnonzero(~same) + 1
    return np.split(order, cuts)


def _bursts(lines: pd.DataFrame) -> list[np.ndarray]:
    return _runs(lines.timestamp.astype("int64").to_numpy(), lines.message.astype(str).to_numpy(),
                 BURST_GAP_NS)


def _episodes(lines: pd.DataFrame) -> list[np.ndarray]:
    return _runs(lines.timestamp.astype("int64").to_numpy(), None, EPISODE_GAP_NS)


def a1_donor_lines(case: CleanCase, container: str) -> pd.DataFrame:
    """A1's content (K22 revision): the container's lines in the same post-fault window of the
    **next repetition of the same development template** (sibling directory, cyclic), so no A1
    line copies a line of its own file. A sibling of a development template is a development case
    by construction (the split is by template)."""
    reps = sorted(p for p in case.case_dir.parent.iterdir() if p.is_dir() and p != case.case_dir)
    if not reps:
        raise RuntimeError(f"{case.case_id}: no other repetition of {case.template} for A1")
    later = [p for p in reps if p.name > case.case_dir.name]
    donor = (later or reps)[0]
    inj = int((donor / "inject_time.txt").read_text().split()[0])
    span = case.window[1] - case.inject_time
    lg = pd.read_csv(donor / "logs.csv", low_memory=False, keep_default_na=False)
    lg = lg[lg.container_name == container]
    sec = lg.timestamp.astype("int64") // 10**9
    out = lg[(sec >= inj) & (sec < inj + span)].copy()
    out["_rel"] = out.timestamp.astype("int64") - inj * 10**9
    return out.reset_index(drop=True)


def _log_inserts(case: CleanCase, logs: pd.DataFrame, target: str, family: str, dose: CaseDose,
                 rng: np.random.Generator) -> list[dict]:
    """New log rows for F1, F4 and A1 (K22 revision, 27 Nov; senior review changes 3-5).

    *Shape.* F1/F4: each forged event replays the shape of a genuine **burst** (one message, gaps
    <= 1 s) of the target container, drawn uniformly over bursts, with the forged error text on
    every line, so a forged error repeats as the container's own lines do. A1: each event is a
    genuine **episode** (the container's lines with gaps <= 10 ms, e.g. payment's Charge +
    Transaction pair) taken from ``a1_donor_lines``, replayed whole (message, level, req_path,
    error) with its own offsets.

    *Placement.* An event starts after the end of a random genuine episode of the container in the
    window, by a gap drawn from the container's genuine episode-to-next-episode gaps, and only where
    no genuine line of the container is within 10 ms (so it is an episode of its own). No line sits
    next to a copy of itself. The dose's line count is kept exactly (the last event is truncated),
    inside the family's window."""
    mine = logs[logs.container_name == log_name(target)]
    lo_s, hi_s = ((case.inject_time - F4_WINDOW[0], case.inject_time - F4_WINDOW[1])
                  if family == "F4" else (case.inject_time, case.window[1]))
    lo_ns, hi_ns = lo_s * 10**9, hi_s * 10**9
    sec = mine.timestamp // 10**9
    own = mine[(sec >= lo_s) & (sec < hi_s)].copy()
    if own.empty:
        return []
    own["_ts"] = own.timestamp.astype("int64")
    own = own.sort_values("_ts", kind="stable").reset_index(drop=True)
    ots = own._ts.to_numpy()
    eps = _episodes(own)
    e_end = np.sort(np.array([int(own.timestamp.iloc[e[-1]]) for e in eps], dtype=np.int64))
    e_start = np.sort(np.array([int(own.timestamp.iloc[e[0]]) for e in eps], dtype=np.int64))
    gaps = e_start[1:] - e_end[:-1]
    gaps = gaps[gaps > EPISODE_GAP_NS]
    multi = [e for e in eps if len(e) > 1]
    # pairing: the share of the container's lines with another line within 1 ms (payment ~0.99,
    # currency ~0.10, email ~0.04) is the chance a forged error is logged right after a request's
    # own lines, at one of the container's within-episode gaps of <= 1 ms
    d = np.diff(ots)
    near = np.minimum(np.r_[np.inf, d], np.r_[d, np.inf])
    p_attach = float((near <= 1_000_000).mean())
    intra = np.concatenate([np.diff(ots[e]) for e in multi]) if multi else np.array([], np.int64)
    intra = intra[intra <= 1_000_000]
    if family == "A1":
        src = a1_donor_lines(case, log_name(target))
        if src.empty:
            return []
        shapes = _episodes(src)
        sts = src.timestamp.astype("int64").to_numpy()
    else:
        src = own
        shapes = _bursts(own)
        sts = own.timestamp.astype("int64").to_numpy()
    n = dose.f1_lines
    upper = _level_case(mine.level) == "upper"
    rows: list[dict] = []
    placed: dict[str, list[int]] = {}       # forged message -> times already written
    event = tries = 0
    while len(rows) < n:
        tries += 1
        if tries > 100 * n:
            raise RuntimeError(f"{case.case_id}: cannot place {family} lines in the window")
        b = shapes[int(rng.integers(len(shapes)))]
        offs = sts[b] - sts[b[0]]
        t0 = None
        if family != "A1" and len(intra) and rng.random() < p_attach:
            # a forged error is logged by a request's handler right after that request's own
            # lines, as the container's multi-line episodes are (e.g. payment's line pairs)
            e = multi[int(rng.integers(len(multi)))]
            cand = int(ots[e[-1]]) + int(intra[int(rng.integers(len(intra)))])
            if lo_ns <= cand and cand + int(offs[-1]) < hi_ns:
                t0 = cand
        for _ in range(0 if t0 is not None else MAX_PLACE_TRIES):
            cand = int(e_end[int(rng.integers(len(e_end)))]) + int(gaps[int(rng.integers(len(gaps)))]) \
                if len(gaps) else int(rng.integers(lo_ns, hi_ns))
            near = np.searchsorted(ots, [cand - EPISODE_GAP_NS, cand + EPISODE_GAP_NS])
            if lo_ns <= cand and cand + int(offs[-1]) < hi_ns and near[0] == near[1]:
                t0 = cand
                break
        if t0 is None:
            continue
        msgs = ([str(src.iloc[int(k)].message) for k in b] if family == "A1" else [None])
        if family != "A1":
            code, desc = GRPC_FAILURES[int(rng.integers(len(GRPC_FAILURES)))]
            msgs = [f"{OPERATION.get(target, target)} failed: rpc error: code = {code} desc = {desc}"]
        # two events of one message within 1 s would merge into a larger burst than was drawn
        if any(abs(t - t0) <= BURST_GAP_NS + int(offs[-1]) for m in set(msgs) for t in placed.get(m, ())):
            continue
        anchor = own.iloc[max(int(np.searchsorted(ots, t0)) - 1, 0)]     # the line before
        if family != "A1":
            message = msgs[0]
            level = "warning" if (family == "F4" and event % 2) else "error"
            level = level.upper() if upper else level
        for k, off in zip(b, offs):
            if len(rows) == n:
                break
            ts = t0 + int(off)
            if family == "A1":         # an ordinary episode of its own kind, from a sibling case
                line = src.iloc[int(k)]
                rows.append({"message": str(line.message), "level": line.level,
                             "req_path": line.req_path, "error": line.error})
            else:
                rows.append({"message": message, "level": level,
                             "req_path": anchor.req_path, "error": anchor.error})
            rows[-1].update(time=_hhmm(ts), timestamp=ts, container_name=log_name(target),
                            cluster_id="", log_template="")
            placed.setdefault(rows[-1]["message"], []).append(ts)
        event += 1
    return rows


# --------------------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------------------

def _write_traces(src: Path, dst: Path, edits: dict[str, dict],
                  inserts: dict[int, list[str]] | None = None) -> int:
    """Genuine lines byte for byte plus ',<sender>'; edited lines re-serialised field by field;
    T1's forged lines (already carrying their sender) written after data row ``i``."""
    done = 0
    inserts = inserts or {}
    with src.open(newline="") as fin, dst.open("w", newline="") as fout:
        header = fin.readline().rstrip("\r\n")
        cols = header.split(",")
        i_sid, i_svc = cols.index("spanID"), cols.index("serviceName")
        i_dur, i_code = cols.index("duration"), cols.index("statusCode")
        i_par = cols.index("parentSpanID")
        i_st, i_stm, i_time = cols.index("startTime"), cols.index("startTimeMillis"), cols.index("time")
        fout.write(header + ",sender\n")
        for row_no, line in enumerate(fin):
            raw = line.rstrip("\r\n")
            f = next(csv.reader([raw]))
            e = edits.get(f[i_sid])
            if e and e.get("_svc", f[i_svc]) != f[i_svc]:
                e = None                      # a duplicated id written by another service: not ours
            sender = f[i_svc]                 # the collector's identity: the writer, before any edit
            if e:
                if "duration" in e:
                    f[i_dur] = str(e["duration"])
                if "statusCode" in e:
                    f[i_code] = e["statusCode"]
                if "parentSpanID" in e:
                    f[i_par] = e["parentSpanID"]
                for k, i in (("startTime", i_st), ("startTimeMillis", i_stm), ("time", i_time),
                             ("serviceName", i_svc)):
                    if k in e:
                        f[i] = e[k]
                buf = io.StringIO()
                csv.writer(buf, lineterminator="").writerow(f)
                raw = buf.getvalue()
                done += 1
            fout.write(f"{raw},{sender}\n")
            for extra in inserts.get(row_no, ()):
                fout.write(extra)
    return done


def _write_logs(logs: pd.DataFrame, dst: Path, inserts: list[dict],
                edits: dict[int, dict] | None = None) -> None:
    """``edits`` (AF1) change fields of rows in place, by position; rows are never re-sorted for
    them, so a row keeps its position and can be compared with the clean case's."""
    out = logs.copy()
    out["sender"] = out.container_name
    for i, e in (edits or {}).items():
        for k, v in e.items():
            out.iat[i, out.columns.get_loc(k)] = v
    if inserts:
        new = pd.DataFrame(inserts)
        new["sender"] = new.container_name
        out = pd.concat([out, new[out.columns]], ignore_index=True)
        out = out.sort_values("timestamp", kind="stable")
    out.to_csv(dst, index=False)


def _derive(case_dir: Path, python: str) -> None:
    r = subprocess.run([python, "-m", "RCAEval.convert", "derive", str(case_dir)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"derive failed for {case_dir}: {r.stderr.strip()[-600:]}")


def _achieved(case: CleanCase, out: Path, target: str) -> dict:
    """The dose as delivered, measured on the overlay's own spans (never assumed)."""
    ov = CleanCase(case_id=case.case_id, case_dir=out, task=case.task, root_cause=case.root_cause,
                   fault=case.fault, template=case.template)
    lo, hi = case.inject_time * 1_000_000, case.window[1] * 1_000_000
    # F2c/FSc unlink their spans from the caller, so the overlay no longer sees them as server
    # spans; the dose is still measured on them (by their clean server-span ids).
    sp = ov.spans
    sp["is_server"] = sp.is_server | ((sp.serviceName == target)
                                      & sp.spanID.isin(case.server_spans(target).spanID))
    s = ov.server_spans(target)
    s = s[(s.startTime >= lo) & (s.startTime < hi)]
    share = float((s.statusCode.fillna(0) != 0).mean()) if len(s) else None
    ratio = ov.p95_ratio(target)
    return {"achieved_post_nonok_share": None if share is None else round(share, 4),
            "achieved_p95_ratio": None if ratio is None else round(ratio, 4),
            "achieved_post_server_spans": int(len(s))}


def harness_python() -> str:
    """The interpreter that has RCAEval's convert extra (drain3). Configurable because this
    package does not depend on it; P1 used the G0 pilot's environment."""
    return os.environ.get("PROVRCA_HARNESS_PYTHON", sys.executable)


# --------------------------------------------------------------------------------------------
# The function
# --------------------------------------------------------------------------------------------

def build_variant(case: CleanCase, *, target: str | None, family: str, threat: str = "T2",
                  dose_level: str = "high", seed: int, out_root: Path, sealed_root: Path,
                  checks: list[CandidateCheck] | None = None,
                  python: str | None = None) -> BuildResult:
    if family not in FAMILIES:
        raise ValueError(f"family must be one of {FAMILIES}")
    if threat not in ("T1", "T2") or (threat == "T1" and family not in T1_FAMILIES):
        raise ValueError(f"threat {threat} is not built for {family} (T1: {sorted(T1_FAMILIES)})")
    if family != "clean" and not target:
        raise ValueError("an overlay needs a target")
    if family in HONEST and (threat != "T2" or dose_level not in HONEST[family]):
        raise ValueError(f"{family} is built as T2 with level {HONEST[family]}")
    variant = variant_name(family, threat, dose_level, seed)
    out = Path(out_root) / case.case_id / variant
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    for name in HARDLINK:
        if (case.case_dir / name).exists():
            os.link(case.case_dir / name, out / name)
    for name in COPY:
        shutil.copy2(case.case_dir / name, out / name)

    dose = dose_for(case, target, dose_level) if family not in ("clean", *HONEST) else None
    rng = np.random.default_rng(seed_for(case.case_id, variant, target))
    res = BuildResult(case.case_id, variant, target, family, dose, out)

    spans_before = None
    edits: dict[str, dict] = {}
    escape: dict[str, str] = {}
    t1 = None
    af1 = af2 = None
    logs = pd.read_csv(case.case_dir / "logs.csv", low_memory=False, keep_default_na=False)
    if family == "AF1":
        af1 = honest.af1_edits(case, target, dose_level, logs)
        edits = {sid: {**e, "_svc": target} for sid, e in af1.span_edits.items()}
        res.extra.update(af1_delta_us=af1.delta_us, af1_spans=len(af1.span_edits),
                         af1_log_lines=len(af1.log_edits), af1_co_located=af1.co_located)
    elif family == "AF2":
        af2 = honest.af2_edits(case, target, rng)
        edits = {sid: {**e, "_svc": af2.b} for sid, e in af2.span_edits.items()}
        res.extra.update(af2_b=af2.b, af2_capped_by_b=af2.capped_by_b, af2_candidates=af2.candidates,
                         af2_excluded=af2.excluded, af2_requested=af2.requested,
                         af2_requests=af2.requests, af2_spans=len(af2.span_edits),
                         af2_requests_pre_post=list(af2.pre_post))
    elif threat == "T1":
        t1 = adaptive.t1_inserts(case, target, family, dose, rng)
        res.extra.update(t1_requested=t1.requested, t1_written=len(t1.server_ids),
                         t1_replays=t1.replays, t1_distinct_traces=t1.distinct_traces,
                         t1_eligible_traces=t1.eligible_traces, t1_witnesses=len(t1.witness_ids),
                         t1_skipped_no_fit=t1.skipped_no_fit, t1_skipped_rc_cap=t1.skipped_rc_cap,
                         t1_rc_added=t1.rc_added, t1_rc_window=t1.rc_window,
                         t1_volume_ratio=t1.volume_ratio)
    elif family == "F3c":
        f3c = adaptive.f3c_edits(case, target, dose.f3_factor, rng)
        edits, escape = f3c.edits, f3c.escape
        res.extra["f3c_gap_source"] = sorted(f3c.gap_source)
    elif family in SPAN_FAMILIES:
        edits = _span_edits(case, target, family, dose, rng)
        if family in ESCAPE_OF:
            escape = {sid: ESCAPE_OF[family] for sid in edits}
    if edits and not (af1 or af2):
        spans_before = case.spans.set_index("spanID").loc[list(edits)]
    res.edited = _write_traces(case.case_dir / "traces.csv", out / "traces.csv", edits,
                               t1.lines if t1 else None)

    inserts = (_log_inserts(case, logs, target, family, dose, rng)
               if family in LOG_FAMILIES else [])
    res.injected = len(inserts)
    _write_logs(logs, out / "logs.csv", inserts, af1.log_edits if af1 else None)

    _derive(out, python or harness_python())

    # labels: one per edited or injected record
    base = dict(case_id=case.case_id, variant=variant, target=target or "none",
                family=family if family != "clean" else "A1", threat_model=threat,
                dose=dose_level, seed=seed, fabricated=True)
    if family in HONEST:
        res.labels = _honest_labels(case, base, af1, af2)
        kinds = [lab["record_id"].split(":")[0] for lab in res.labels]
        res.extra["labels_by_kind"] = {k: kinds.count(k) for k in sorted(set(kinds))}
    elif family != "clean":
        pairs = adaptive.clean_pairs(str(case.case_dir)) if (edits or t1) else None
        for sid, e in edits.items():
            orig = spans_before.loc[sid]
            false_status = "statusCode" in e and not (orig.statusCode == float(e["statusCode"]))
            false_duration = "duration" in e and e["duration"] != int(orig.duration)
            clean_ok = bool(sid in pairs.index and any(pairs.at[sid, r] for r in RULE_OF[family]))
            esc = escape.get(sid, "none")
            res.labels.append(LabelRecord(
                **base, record_id=f"span:{sid}",
                factually_false=bool(false_status or false_duration),
                rule_applicable=clean_ok and esc in ("none", "R1_cap"),
                rule_applicable_clean=clean_ok, escape=esc).model_dump())
        if t1:
            for sid in t1.server_ids:
                ok = bool(t1.applicable.get(sid, False))
                res.labels.append(LabelRecord(**base, record_id=f"span:{sid}", factually_false=True,
                                              rule_applicable=ok, rule_applicable_clean=ok,
                                              escape="none").model_dump())
            for cid in t1.client_ids + t1.witness_ids:
                res.labels.append(LabelRecord(**base, record_id=f"span:{cid}", factually_false=True,
                                              rule_applicable=None, rule_applicable_clean=None,
                                              escape="witness").model_dump())
        for k, row in enumerate(inserts):
            res.labels.append(LabelRecord(**base, record_id=f"log:{row['timestamp']}:{k}",
                                          factually_false=family != "A1", rule_applicable=False,
                                          rule_applicable_clean=False,
                                          escape="no_log_rule").model_dump())
        res.extra.update(_achieved(case, out, target))
    _seal(res, Path(sealed_root), checks)
    return res


def _honest_labels(case: CleanCase, base: dict, af1, af2) -> list[dict]:
    """AF1/AF2 labels (step 5 review, item 8). These are genuine records made by no attacker, so
    ``fabricated`` is False (the contract allows that for AF1/AF2 only). Each record is factually
    false in one field: AF1 its time, AF2 its writer. No rule reads either field, so
    ``rule_applicable`` is False and ``escape`` names why: ``no_time_rule`` (AF1) or
    ``identity_claim`` (AF2, which only attribution can check). ``rule_applicable_clean`` says
    whether R1 or R2 applied to the record's clean pair (server spans; False for other spans and
    for logs). The scorer must keep AF records out of every F-family metric and out of the
    fabricated denominators; they are reported per family (review item 8).

    AF1 log ids are ``log:<row>``, the same key ``provrca.chain.judge_logs`` gives, because AF1
    never re-sorts the log file (review item 2)."""
    pairs = adaptive.clean_pairs(str(case.case_dir))
    applies = (pairs.r1 | pairs.r2)
    applies = applies[~applies.index.duplicated(keep=False)].to_dict()
    base = {**base, "fabricated": False}
    esc = "no_time_rule" if af1 else "identity_claim"
    out = []
    for sid in (af1.span_edits if af1 else af2.span_edits):
        out.append(LabelRecord(**base, record_id=f"span:{sid}", factually_false=True,
                               rule_applicable=False,
                               rule_applicable_clean=bool(applies.get(sid, False)),
                               escape=esc).model_dump())
    if af1:
        for i in af1.log_edits:
            out.append(LabelRecord(**base, record_id=f"log:{i}", factually_false=True,
                                   rule_applicable=False, rule_applicable_clean=False,
                                   escape=esc).model_dump())
    return out


def _seal(res: BuildResult, sealed_root: Path, checks: list[CandidateCheck] | None) -> None:
    d = sealed_root / res.case_id
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{res.variant}.labels.jsonl").open("w") as f:
        for lab in res.labels:
            f.write(json.dumps(lab, sort_keys=True) + "\n")
    meta = {"case_id": res.case_id, "variant": res.variant, "target": res.target,
            "family": res.family, "dose": asdict(res.dose) if res.dose else None,
            "edited_spans": res.edited, "injected_lines": res.injected, **res.extra,
            "target_checks": [asdict(c) for c in checks] if checks else None}
    (d / f"{res.variant}.meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
