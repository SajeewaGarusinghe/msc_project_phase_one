"""Pre-flight for the K22 revision (senior review change 5), before any overlay is regenerated.

Calls the revised insertion routines directly (no files written, no derive) on development cases
and compares the inserted records with the genuine ones of the same file:

* logs (F1, F4, A1; both doses): per target container, the share of lines with the same message
  within 1 s (K22's ``dup_msg_1s``), with any other line of the container within 1 ms
  (``near_1ms``), and whose exact message appears elsewhere in the window (``dup_window``).
  Pre-registered check: |forged - genuine| <= 0.05 for ``dup_msg_1s`` and ``near_1ms``.
  Round 2 (review change 5): also the single-feature AUC, forged vs genuine (genuine sampled to
  the forged count per case, as K22 does), of K22's ``episode_size``, ``near_gap_log10``,
  ``offset_s`` and ``dup_msg_1s``; check |AUC - 0.5| <= 0.03 pooled per family and dose, reported
  per target service. F1/F4 companion lines are neither forged nor genuine.
* T1 (F2, F3; both doses): replayed traces that are exact twins of a genuine trace (duration vector
  and relative offsets), against the genuine rate among genuine traces of >= 10 spans (check:
  <= genuine + 0.01); claims' ``n_children`` and ``trace_size`` against the genuine target server
  spans; per-service volume ratio, and the root cause's (reported; the cap is off). Round 2: the
  single-feature AUC of K22's ``burst_100ms`` and ``load_1s`` and of "another target server start
  within 5 ms", claims vs genuine target server spans in the window (same check, pooled).

    uv run python scripts/p2_k22_preflight.py --out <dir> [--cases re2-ob-0001 ...]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from sklearn.metrics import roc_auc_score

from provrca_bench.framebench import adaptive, build as fb, k22
from provrca_bench.framebench.case import load_dev_cases, log_name
from provrca_bench.framebench.doses import dose_for
from provrca_bench.framebench.target import seed_for

DATASET = Path("parity/dataset-rebuilt")
TASKS = Path("parity/tasks-rebuilt/re2-ob/cases")
ANSWERS = Path("parity/tasks-rebuilt/re2-ob.answers.jsonl")
TARGETS = Path("sealed/framebench-dev/targets.csv")
SEED = 20261125


def _log_stats(lg: pd.DataFrame, rows: np.ndarray) -> dict:
    ts = lg._ts.to_numpy()
    msg = lg.message.astype(str).to_numpy()
    order = np.argsort(ts, kind="stable")
    rank = np.empty_like(order)
    rank[order] = np.arange(len(order))
    sts = ts[order]
    near, dup1 = [], []
    by_msg = pd.Series(np.arange(len(lg))).groupby(msg).apply(lambda s: np.sort(ts[s.to_numpy()]))
    for r in rows:
        i = rank[r]
        g = min(sts[i] - sts[i - 1] if i > 0 else 10**18,
                sts[i + 1] - sts[i] if i + 1 < len(sts) else 10**18)
        near.append(g <= 1_000_000)
        same = by_msg[msg[r]]
        k = np.searchsorted(same, [ts[r] - 10**9, ts[r] + 10**9 + 1])
        dup1.append(k[1] - k[0] > 1)
    cnt = pd.Series(msg).value_counts()
    return {"n": len(rows), "dup_msg_1s": float(np.mean(dup1)) if rows.size else np.nan,
            "near_1ms": float(np.mean(near)) if rows.size else np.nan,
            "dup_window": float(np.mean(cnt.reindex(msg[rows]).to_numpy() > 1)) if rows.size else np.nan}


def _wilson(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def summarise_logs(logs: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
    """Pooled over cases (line-weighted). The pre-registered check: for dup_msg_1s and near_1ms, the
    forged share is within +-0.05 of the genuine one, or its 95% Wilson interval overlaps
    genuine +- 0.05 (sampling error at 90-180 forged lines per cell). dup_window is reported only."""
    rows, ok = [], True
    for (fam, dose, tgt), g in logs.groupby(["family", "dose", "target"]):
        r = {"family": fam, "dose": dose, "target": tgt}
        for kind in ("forged", "genuine"):
            k = g[g.kind == kind]
            r[f"n_{kind}"] = int(k.n.sum())
            for m in ("dup_msg_1s", "near_1ms", "dup_window"):
                r[f"{m}_{kind}"] = float((k[m] * k.n).sum() / max(k.n.sum(), 1))
        for m in ("dup_msg_1s", "near_1ms"):
            f, gen, n = r[f"{m}_forged"], r[f"{m}_genuine"], r["n_forged"]
            lo, hi = _wilson(f, n)
            passed = abs(f - gen) <= 0.05 or (lo <= gen + 0.05 and hi >= gen - 0.05)
            r[f"{m}_diff"], r[f"{m}_pass"] = round(f - gen, 3), passed
            ok &= passed
        rows.append(r)
    return pd.DataFrame(rows).round(3), ok


LOG_AUC = ["episode_size", "near_gap_log10", "offset_s", "dup_msg_1s"]
SPAN_AUC = ["burst_100ms", "load_1s", "nn_5ms"]
AUC_TOL = 0.03


def logs_case(case, target) -> tuple[list[dict], list[pd.DataFrame]]:
    logs = pd.read_csv(case.case_dir / "logs.csv", low_memory=False, keep_default_na=False)
    out, feats = [], []
    for family in ("F1", "F4", "A1"):
        for level in ("high", "low"):
            dose = dose_for(case, target, level)
            variant = fb.variant_name(family, "T2", level, SEED)
            rng = np.random.default_rng(seed_for(case.case_id, variant, target))
            ins = fb._log_inserts(case, logs, target, family, dose, rng)
            if not ins:
                continue
            new = pd.DataFrame(ins)
            wit = new.get("_witness", pd.Series(False, index=new.index)).astype(bool)
            lg = pd.concat([logs, new[logs.columns]], ignore_index=True)
            lg = lg[lg.container_name == log_name(target)].copy()
            lg["_ts"] = lg.timestamp.astype("int64")
            lo, hi = ((case.inject_time - 300, case.inject_time - 120) if family == "F4"
                      else (case.inject_time, case.window[1]))
            sec = lg._ts // 10**9
            lg = lg[(sec >= lo) & (sec < hi)].reset_index(drop=True)
            forged = lg._ts.isin(set(new.timestamp[~wit])).to_numpy()
            comp = lg._ts.isin(set(new.timestamp[wit])).to_numpy()
            for kind, rows in (("forged", np.flatnonzero(forged)),
                               ("genuine", np.flatnonzero(~forged & ~comp))):
                out.append({"case_id": case.case_id, "target": target, "family": family,
                            "dose": level, "kind": kind, **_log_stats(lg, rows)})
            # K22's own feature code on the same frame (the container, in the window)
            lg["_dup"] = k22._dups(lg)
            allm = np.ones(len(lg), dtype=bool)
            pos = np.flatnonzero(forged)
            gen = np.flatnonzero(~forged & ~comp)
            gen = np.sort(rng.choice(gen, min(len(gen), len(pos)), replace=False))
            for y, rows in ((1, pos), (0, gen)):
                f = pd.concat([k22.log_features(lg, rows, pd.Series(dtype=int), case.inject_time,
                                                bool(y)),
                               k22.log_supp(lg, allm, allm, rows)], axis=1)
                f = f[LOG_AUC]
                f.insert(0, "y", y)
                for k2, v2 in (("case_id", case.case_id), ("target", target), ("family", family),
                               ("dose", level)):
                    f.insert(0, k2, v2)
                feats.append(f)
    return out, feats


def _shape(df: pd.DataFrame) -> tuple:
    st = df.startTime.astype("int64").to_numpy()
    return tuple(np.r_[df.duration.astype("int64").to_numpy(), st - st.min()])


def t1_case(case, target) -> tuple[list[dict], list[pd.DataFrame]]:
    tr = adaptive._clean_frame(str(case.case_dir))
    lo, hi = case.inject_time * 10**6, case.window[1] * 10**6
    st = tr.startTime.astype("int64")
    gen = tr[(st >= lo) & (st < hi)]
    sizes = gen.traceID.value_counts()
    big = sizes[sizes >= 10].index
    shapes = gen[gen.traceID.isin(big)].groupby("traceID", sort=False).apply(_shape)
    gen_twin = float(shapes.duplicated(keep=False).mean()) if len(shapes) else np.nan
    gset = set(shapes)
    srv_all = case.server_spans(target)
    srv = srv_all[(srv_all.startTime >= lo) & (srv_all.startTime < hi)]
    feats = []
    kids = tr.parentSpanID.value_counts()
    tsize = tr.traceID.value_counts()
    tid = dict(zip(tr.spanID, tr.traceID))
    g_child = float((srv.spanID.map(kids).fillna(0) > 0).mean())
    g_size = float(srv.spanID.map(tid).map(tsize).median())
    out = []
    for family in ("F2", "F3"):
        for level in ("high", "low"):
            dose = dose_for(case, target, level)
            variant = fb.variant_name(family, "T1", level, SEED)
            rng = np.random.default_rng(seed_for(case.case_id, variant, target))
            res = adaptive.t1_inserts(case, target, family, dose, rng)
            lines = [l for ls in res.lines.values() for l in ls]
            if not lines:
                out.append({"case_id": case.case_id, "target": target, "family": family,
                            "dose": level, "claims": 0})
                continue
            cols = list(tr.columns) + ["sender"]
            f = pd.read_csv(io.StringIO("".join(lines)), names=cols, dtype=str,
                            keep_default_na=False)
            # K22's own span code on the overlay (genuine + copies)
            ov = pd.concat([tr.astype(str), f[list(tr.columns)]], ignore_index=True)
            ov_st = pd.to_numeric(ov.startTime)
            cl_rows = ov.index[ov.spanID.isin(set(res.server_ids))]
            g_rows = ov.index[:len(tr)][tr.spanID.isin(set(srv.spanID)).to_numpy()]
            g_rows = pd.Index(np.sort(rng.choice(g_rows, min(len(g_rows), len(cl_rows)),
                                                 replace=False)))
            s_all = np.sort(ov_st[ov.index.isin(cl_rows) | ov.index.isin(
                ov.index[:len(tr)][tr.spanID.isin(set(srv_all.spanID)).to_numpy()])].to_numpy())
            for y, rows in ((1, cl_rows), (0, g_rows)):
                ex = k22.span_extra(ov, rows, target)
                sp = k22.span_supp(ov, rows)
                x = ov_st.loc[rows].to_numpy()
                i = np.searchsorted(s_all, x)
                left = np.where(i > 0, x - s_all[np.maximum(i - 1, 0)], 10**12)
                right = np.where(i + 1 < len(s_all), s_all[np.minimum(i + 1, len(s_all) - 1)] - x,
                                 10**12)
                fr = pd.DataFrame({"burst_100ms": ex.burst_100ms.values,
                                   "load_1s": sp.load_1s.values,
                                   "nn_5ms": (np.minimum(left, right) <= 5_000).astype(int)})
                fr.insert(0, "y", y)
                for k2, v2 in (("case_id", case.case_id), ("target", target), ("family", family),
                               ("dose", level)):
                    fr.insert(0, k2, v2)
                feats.append(fr)
            fsh = f.groupby("traceID", sort=False).apply(_shape)
            fsz = f.traceID.value_counts()
            fk = f.parentSpanID[f.parentSpanID != ""].value_counts()
            claims = f[f.spanID.isin(set(res.server_ids))]
            out.append({
                "case_id": case.case_id, "target": target, "family": family, "dose": level,
                "claims": len(res.server_ids), "requested": res.requested, "replays": res.replays,
                "distinct_traces": res.distinct_traces, "eligible_traces": res.eligible_traces,
                "skipped_rc_cap": res.skipped_rc_cap, "skipped_no_fit": res.skipped_no_fit,
                "twin_of_genuine": float(fsh[fsz.reindex(fsh.index).to_numpy() >= 10]
                                         .isin(gset).mean()) if (fsz >= 10).any() else 0.0,
                "twin_among_replays": float(fsh.duplicated(keep=False).mean()),
                "genuine_twin_rate": gen_twin,
                "claim_has_child": float((claims.spanID.map(fk).fillna(0) > 0).mean()),
                "genuine_has_child": g_child,
                "claim_trace_size_med": float(claims.traceID.map(fsz).median()),
                "genuine_trace_size_med": g_size,
                "rc": case.root_cause, "rc_ratio": res.volume_ratio.get(case.root_cause, 1.0),
                "rc_added": res.rc_added, "rc_window": res.rc_window,
                "max_ratio_service": max(res.volume_ratio, key=res.volume_ratio.get),
                "max_ratio": max(res.volume_ratio.values()),
                "volume_ratio": json.dumps(res.volume_ratio, sort_keys=True)})
    return out, feats


def one(args):
    case, target, what = args
    return (logs_case(case, target) if what == "logs" else ([], [])), \
           (t1_case(case, target) if what == "t1" else ([], []))


def auc_table(feats: pd.DataFrame, cols: list[str]) -> tuple[pd.DataFrame, bool]:
    """Single-feature AUC, forged (y=1) vs genuine, pooled per family/dose (the check) and per
    target service (reported)."""
    rows, ok = [], True
    for keys, g in feats.groupby(["family", "dose"]):
        for scope, sub in [("pooled", g)] + [(t, h) for t, h in g.groupby("target")]:
            r = {"family": keys[0], "dose": keys[1], "scope": scope,
                 "n_forged": int(sub.y.sum()), "n_genuine": int((sub.y == 0).sum())}
            for c in cols:
                v = sub[c].astype(float)
                r[c] = (round(float(roc_auc_score(sub.y, v)), 3)
                        if sub.y.nunique() == 2 and v.nunique() > 1 else 0.5)
            if scope == "pooled":
                r["pass"] = all(abs(r[c] - 0.5) <= AUC_TOL for c in cols)
                ok &= r["pass"]
            rows.append(r)
    return pd.DataFrame(rows), ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cases", nargs="*")
    ap.add_argument("--only", choices=["logs", "t1", "both"], default="both")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args(argv)
    dev = {json.loads(l)["source"].split("/")[0] for l in ANSWERS.read_text().splitlines()}
    cases = load_dev_cases(dataset_root=DATASET, tasks_root=TASKS, answers_jsonl=ANSWERS,
                           dev_templates=dev)
    if a.cases:
        cases = [c for c in cases if c.case_id in set(a.cases)]
    tg = {r["case_id"]: r["service"] for r in csv.DictReader(TARGETS.open()) if r["drawn"] == "True"}
    whats = ["logs", "t1"] if a.only == "both" else [a.only]
    jobs = [(c, tg[c.case_id], w) for c in cases for w in whats]
    a.out.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(a.workers) as pool:
        res = list(pool.map(one, jobs))
    logs = pd.DataFrame([r for (l, _), _ in res for r in l])
    t1 = pd.DataFrame([r for _, (t, _) in res for r in t])
    lf = [f for (_, fs), _ in res for f in fs]
    tf = [f for _, (_, fs) in res for f in fs]
    for name, fs, cols in (("logs", lf, LOG_AUC), ("t1", tf, SPAN_AUC)):
        if fs:
            tab, ok = auc_table(pd.concat(fs, ignore_index=True), cols)
            tab.to_csv(a.out / f"preflight_{name}_auc.csv", index=False)
            print(tab.to_string(index=False))
            print(f"{name.upper()} AUC CHECK (|AUC-0.5| <= {AUC_TOL} pooled):", "PASS" if ok else "FAIL")
    if len(logs):
        logs.to_csv(a.out / "preflight_logs.csv", index=False)
        agg, ok = summarise_logs(logs)
        agg.to_csv(a.out / "preflight_logs_summary.csv", index=False)
        print(agg.to_string(index=False))
        print("LOG CHECK:", "PASS" if ok else "FAIL")
    if len(t1):
        t1.to_csv(a.out / "preflight_t1.csv", index=False)
        cols = ["case_id", "target", "family", "dose", "claims", "requested", "replays",
                "twin_of_genuine", "genuine_twin_rate", "claim_has_child", "genuine_has_child",
                "claim_trace_size_med", "genuine_trace_size_med", "rc", "rc_ratio",
                "max_ratio_service", "max_ratio", "skipped_rc_cap"]
        print(t1[[c for c in cols if c in t1]].to_string(index=False))
        ok = (t1.twin_of_genuine.fillna(0) <= t1.genuine_twin_rate.fillna(0) + 0.01).all()
        print("root-cause volume ratio (reported, not checked: the cap is off):",
              t1.groupby(["family", "dose"]).rc_ratio.describe().round(3).to_string())
        print("T1 CHECK:", "PASS" if ok else "FAIL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
