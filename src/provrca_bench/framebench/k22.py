"""The full K22 distinguishability sweep (proposal §06) on FrameBench's development overlays.

Pre-registered in Msc-project's decisions log, 27 Nov ("K22 sweep: pre-registration"), before this
ran; the run records that commit. Step 4b's ``distinguish.py`` is left untouched so its numbers can
be reproduced exactly; this module reuses its span features (step 4b's 16, the primary ``full``
set) and adds:

* F3, F3c (T2), whose forged value is the duration;
* T1-F2, T1-F3: forged server claims against the target's genuine server spans **in the same
  overlay file**, with a ``minus_forged_structure`` mode, because T1's trace structure is a
  builder choice, not a forged value;
* the log families F1, F4 and the A1 control: injected lines against the target container's
  unlabelled lines **in the same overlay file and window**. Drain3's integer ``cluster_id`` is never
  used: re-deriving an overlay renumbers genuine lines too, so it separates files, not forgeries;
* AF1, AF2 (descriptive only);
* extended timing features (``start_vs_parent_us``, ``end_vs_parent_us``, ``burst_100ms``),
  reported for every span family but able to trigger only for T1, where they are builder-made
  (for T2 span families they encode R1's own cross-record check).

Every AUC is reported as the mean over GroupKFold(4) by template, with a case-clustered bootstrap
95% interval on the out-of-fold scores, and per target service. The two triggers and the A1 gate are
applied mechanically (``verdicts.csv``).
"""
from __future__ import annotations

import argparse
import json
import subprocess
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import GroupKFold

from provrca_bench.framebench.case import load_dev_cases, log_name
from provrca_bench.framebench.distinguish import (DURATION, FORGED as FORGED_4B,
                                                  MAX_PER_CLASS_PER_CASE, SEED, STATUS, read,
                                                  features)

SPAN_T2 = ("F2", "F2c", "F2cD", "FS", "FSc", "F3", "F3c")
T1 = ("F2", "F3")
LOGS = ("F1", "F4", "A1")
AF = ("AF1", "AF2")
STRUCT = ["parent_service", "parent_method", "is_server", "parent_in_file", "is_root",
          "method_eq_parent", "n_children", "trace_size"]
TIMING = ["start_vs_parent_us", "end_vs_parent_us", "burst_100ms"]
FORGED = {**FORGED_4B, "F3": DURATION, "F3c": DURATION,
          "T1-F2": STATUS, "T1-F3": DURATION, "F1": [], "F4": [], "A1": []}
LOG_FORGED = ["level", "template", "template_seen", "msg_len"]
DECLARED = {"F1", "F2", "F2c", "F2cD", "FS", "FSc", "T1-F2"}
UNDECLARED = {"F3", "F3c", "T1-F3", "F4", "A1"}
CATEGORICAL = {"service", "method", "operation", "parent_service", "parent_method", "level",
               "template"}
F4_WINDOW = (300, 120)
BOOT = int(__import__("os").environ.get("K22_BOOT", "1000"))


# ------------------------------------------------------------------ span helpers
def span_extra(df: pd.DataFrame, rows: pd.Index, target: str) -> pd.DataFrame:
    """Timing against the parent span and the burst count, from the whole file."""
    sid = df.spanID.astype(str)
    st = pd.to_numeric(df.startTime, errors="coerce")
    du = pd.to_numeric(df.duration, errors="coerce")
    st_of, end_of = dict(zip(sid, st)), dict(zip(sid, st + du))
    x = df.loc[rows]
    pid = x.parentSpanID.fillna("").astype(str)
    p_st, p_end = pid.map(st_of), pid.map(end_of)
    xs = pd.to_numeric(x.startTime, errors="coerce")
    xe = xs + pd.to_numeric(x.duration, errors="coerce")
    svc_of = dict(zip(sid, df.serviceName.astype(str)))
    p_svc = df.parentSpanID.fillna("").astype(str).map(svc_of)
    srv = np.sort(st[(df.serviceName == target) & p_svc.notna() & (p_svc != target)].to_numpy())
    burst = (np.searchsorted(srv, xs.to_numpy() + 50_000, side="right")
             - np.searchsorted(srv, xs.to_numpy() - 50_000, side="left"))
    return pd.DataFrame({"start_vs_parent_us": (xs - p_st).values,
                         "end_vs_parent_us": (xe - p_end).values, "burst_100ms": burst})


def server_rows(df: pd.DataFrame, target: str, lo: int, hi: int, exclude: set[str]) -> pd.Index:
    svc_of = dict(zip(df.spanID, df.serviceName))
    p = df.parentSpanID.map(svc_of)
    return df.index[(df.serviceName == target) & p.notna() & (p != target)
                     & (df.startTime >= lo) & (df.startTime < hi) & ~df.spanID.isin(exclude)]


def span_frame(df_pos, pos, df_neg, neg, inject, target, meta) -> pd.DataFrame:
    X = pd.concat([pd.concat([features(df_pos, pos, inject), span_extra(df_pos, pos, target)], axis=1),
                   pd.concat([features(df_neg, neg, inject), span_extra(df_neg, neg, target)], axis=1)],
                  ignore_index=True)
    X["_y"] = [1] * len(pos) + [0] * len(neg)
    for k, v in meta.items():
        X[k] = v
    return X


# ------------------------------------------------------------------ log helpers
def log_features(lg: pd.DataFrame, rows: np.ndarray, unl_counts: pd.Series, inject: int,
                 positive: bool) -> pd.DataFrame:
    x = lg.iloc[rows]
    tmpl = x.log_template.astype(str)
    seen = tmpl.map(unl_counts).fillna(0).to_numpy() - (0 if positive else 1)
    return pd.DataFrame({
        "level": x.level.astype(str).str.upper().values,
        "template": tmpl.values,
        "template_seen": (seen > 0).astype(int),
        "msg_len": x.message.astype(str).str.len().values,
        "offset_s": (x.timestamp.astype("int64") / 1e9 - inject).values,
        "req_path_empty": (x.req_path.astype(str) == "").astype(int).values,
        "error_empty": (x.error.astype(str) == "").astype(int).values,
        "dup_msg_1s": x["_dup"].values,
        "ts_ns_mod1000": (x.timestamp.astype("int64") % 1000).values,
    })


def _dups(lg: pd.DataFrame) -> np.ndarray:
    """1 if another line of the same container has the identical message within +/-1 s."""
    out = np.zeros(len(lg), dtype=int)
    key = lg.container_name.astype(str) + "\x00" + lg.message.astype(str)
    ts = lg.timestamp.astype("int64").to_numpy()
    order = np.lexsort((ts, key.to_numpy()))
    k, t = key.to_numpy()[order], ts[order]
    same_prev = np.r_[False, (k[1:] == k[:-1]) & (t[1:] - t[:-1] <= 1_000_000_000)]
    same_next = np.r_[same_prev[1:], False]
    out[order] = (same_prev | same_next).astype(int)
    return out


# ------------------------------------------------------------------ one case
def one_case(args) -> tuple[list[pd.DataFrame], list[dict]]:
    case, gen, sealed = args
    rng = np.random.default_rng([SEED, int(case.case_id.split("-")[-1])])
    inject, end = case.inject_time, case.window[1]
    lo, hi = inject * 1_000_000, end * 1_000_000
    cdir = gen / case.case_id
    clean = read(cdir / "clean" / "traces.csv")
    frames, recov = [], []

    def sample(idx, n):
        return idx[np.sort(rng.choice(len(idx), n, replace=False))] if len(idx) > n else idx

    for v in sorted(p.name for p in cdir.iterdir()):
        fam, threat, dose = (v.split("-") + ["", ""])[:3]
        key = f"T1-{fam}" if threat == "T1" else fam
        if not ((threat == "T2" and fam in SPAN_T2 + LOGS + AF) or (threat == "T1" and fam in T1)):
            continue
        labels = [json.loads(l) for l in (sealed / case.case_id / f"{v}.labels.jsonl")
                  .read_text().splitlines()]
        if not labels:
            continue
        target = labels[0]["target"]
        meta = {"_family": key, "_dose": dose, "_template": case.template,
                "_case": case.case_id, "_target": target, "_kind": "log" if fam in LOGS else "span"}
        span_ids = {l["record_id"][5:] for l in labels if l["record_id"].startswith("span:")}
        if fam in LOGS:
            lg = pd.read_csv(cdir / v / "logs.csv", dtype=str, keep_default_na=False, low_memory=False)
            lg["_dup"] = _dups(lg)
            stamps = {int(l["record_id"].split(":")[1]) for l in labels}
            ts = lg.timestamp.astype("int64")
            mine = (lg.container_name == log_name(target)).to_numpy()
            lo_s, hi_s = ((inject - F4_WINDOW[0], inject - F4_WINDOW[1]) if fam == "F4"
                          else (inject, end))
            inwin = ((ts // 10**9 >= lo_s) & (ts // 10**9 < hi_s)).to_numpy()
            is_pos = ts.isin(stamps).to_numpy() & mine
            pos = np.flatnonzero(is_pos)
            neg = np.flatnonzero(mine & inwin & ~is_pos)
            unl = lg.log_template[mine & ~is_pos].astype(str).value_counts()
            n = min(len(pos), len(neg), MAX_PER_CLASS_PER_CASE)
            recov.append({**meta, "variant": v, "labelled": len(stamps), "found": len(pos),
                          "pool": len(neg), "used": n, "pool_rule": "same_file"})
            if n == 0:
                continue
            pos, neg = sample(pos, n), sample(neg, n)
            X = pd.concat([log_features(lg, pos, unl, inject, True),
                           log_features(lg, neg, unl, inject, False)], ignore_index=True)
            X["_y"] = [1] * n + [0] * n
            for k2, v2 in meta.items():
                X[k2] = v2
            frames.append(X)
            continue
        ov = read(cdir / v / "traces.csv")
        pool_rule = "same_file" if (threat == "T1" or fam == "AF2") else "clean_twin_excluding_forged"
        if threat == "T1":
            claims = {l["record_id"][5:] for l in labels
                      if l["record_id"].startswith("span:") and l["escape"] == "none"}
            pos = ov.index[ov.spanID.isin(claims)]
            neg = server_rows(ov, target, lo, hi, span_ids)
            df_neg = ov
        elif fam == "AF2":
            pos = ov.index[ov.spanID.isin(span_ids) & (ov.serviceName == target)
                           & (ov.startTime >= lo) & (ov.startTime < hi)]
            neg = server_rows(ov, target, lo, hi, span_ids)
            df_neg = ov
        elif fam == "AF1":
            pool_rule = "clean_twin"
            pos = ov.index[ov.spanID.isin(span_ids) & (ov.serviceName == target)
                           & (ov.startTime >= lo) & (ov.startTime < hi)]
            neg = clean.index[(clean.serviceName == target) & (clean.startTime >= lo)
                              & (clean.startTime < hi)]
            df_neg = clean
        else:
            claims = {l["record_id"][5:] for l in labels
                      if l["record_id"].startswith("span:") and l["escape"] != "witness"}
            pos = ov.index[ov.spanID.isin(claims)]
            neg = server_rows(clean, target, lo, hi, claims)
            df_neg = clean
            if len(neg) == 0:
                # F3 edits every post-fault target server span in the window, so step 4b's pool
                # (excluding the forged ids) is empty: the only genuine post-fault server spans of
                # the target are those spans' clean originals, sampled independently
                neg = server_rows(clean, target, lo, hi, set())
                pool_rule = "clean_originals"
        n = min(len(pos), len(neg), MAX_PER_CLASS_PER_CASE)
        recov.append({**meta, "variant": v, "labelled": len(span_ids), "found": len(pos),
                      "pool": len(neg), "used": n, "pool_rule": pool_rule})
        if n == 0:
            continue
        frames.append(span_frame(ov, sample(pos, n), df_neg, sample(neg, n), inject, target, meta))
    return frames, recov


# ------------------------------------------------------------------ evaluation
def modes(fam: str, kind: str) -> dict[str, list[str] | None]:
    """mode -> features to drop (None: the mode does not apply). Extended timing features are
    dropped from every non-timing mode, so ``full`` is exactly step 4b's set for spans."""
    if kind == "log":
        out = {"full": []}
        if fam in ("F1", "F4"):
            out["minus_forged"] = LOG_FORGED
        return out
    f = FORGED.get(fam, [])
    out = {"full": TIMING, "full+timing": []}
    if fam in AF:
        return out
    out["minus_forged"] = TIMING + f
    out["timing_minus_forged"] = list(f)
    if fam.startswith("T1-"):
        out["minus_forged_structure"] = TIMING + f + STRUCT
    return out


def _boot(y, s, cases, rng) -> tuple[float, float]:
    uc = np.unique(cases)
    idx = {c: np.flatnonzero(cases == c) for c in uc}
    vals = []
    for _ in range(BOOT):
        pick = rng.choice(uc, len(uc), replace=True)
        rows = np.concatenate([idx[c] for c in pick])
        if len(set(y[rows])) == 2:
            vals.append(roc_auc_score(y[rows], s[rows]))
    return float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def evaluate(data: pd.DataFrame, folds: int = 4) -> tuple[list[dict], list[dict]]:
    rows, by_target = [], []
    rng = np.random.default_rng(SEED)
    for (fam, dose), df in data.groupby(["_family", "_dose"]):
        df = df.dropna(axis=1, how="all").copy()
        kind = df["_kind"].iloc[0]
        for col in CATEGORICAL & set(df.columns):
            df[col] = pd.factorize(df[col].astype(str))[0]
        meta_cols = [c for c in df.columns if c.startswith("_")]
        for mode, drop in modes(fam, kind).items():
            X = df.drop(columns=meta_cols + [c for c in drop if c in df.columns])
            y, groups = df["_y"].to_numpy(), df["_template"].to_numpy()
            oof = np.full(len(df), np.nan)
            aucs, fold_n = [], []
            for tr, te in GroupKFold(n_splits=folds).split(X, y, groups):
                if len(set(y[te])) < 2:
                    continue
                clf = HistGradientBoostingClassifier(random_state=SEED, max_iter=200)
                clf.fit(X.iloc[tr], y[tr])
                oof[te] = clf.predict_proba(X.iloc[te])[:, 1]
                aucs.append(roc_auc_score(y[te], oof[te]))
                fold_n.append(int(len(te)))
            ok = ~np.isnan(oof)
            lo_ci, hi_ci = _boot(y[ok], oof[ok], df["_case"].to_numpy()[ok], rng)
            rows.append({"family": fam, "dose": dose, "kind": kind, "features": mode,
                         "n_forged": int((y == 1).sum()), "n_genuine": int((y == 0).sum()),
                         "n_cases": int(df["_case"].nunique()), "n_features": X.shape[1],
                         "folds": len(aucs), "fold_n": "|".join(map(str, fold_n)),
                         "auc_mean": round(float(np.mean(aucs)), 4),
                         "auc_min": round(float(np.min(aucs)), 4),
                         "auc_max": round(float(np.max(aucs)), 4),
                         "boot_lo": round(lo_ci, 4), "boot_hi": round(hi_ci, 4),
                         "dropped": "|".join(drop)})
            if mode in ("full", "minus_forged"):
                for t, g in df[ok].groupby("_target"):
                    yy, ss = g["_y"].to_numpy(), oof[ok][df[ok]["_target"].to_numpy() == t]
                    if len(set(yy)) == 2:
                        by_target.append({"family": fam, "dose": dose, "features": mode,
                                          "target": t, "n": len(g),
                                          "auc": round(float(roc_auc_score(yy, ss)), 4)})
    return rows, by_target


def verdicts(auc: pd.DataFrame) -> pd.DataFrame:
    """The pre-registered rules, applied mechanically."""
    out = []
    get = lambda f, d, m: auc[(auc.family == f) & (auc.dose == d) & (auc.features == m)]  # noqa: E731
    for (fam, dose), _ in auc.groupby(["family", "dose"]):
        r = {"family": fam, "dose": dose, "declared": fam in DECLARED,
             "trigger_a": "", "trigger_b": "", "trigger_b_structure": "", "trigger_b_timing": "",
             "a1_gate": ""}
        full = get(fam, dose, "full").iloc[0]
        if fam in UNDECLARED:
            r["trigger_a"] = bool(full.auc_mean >= 0.75 or full.boot_lo >= 0.70)
        fp = lambda row: bool(row.auc_mean >= 0.55 and row.boot_lo > 0.52)  # noqa: E731
        if fam in DECLARED | UNDECLARED and fam != "A1":
            mf = get(fam, dose, "minus_forged")
            if len(mf):
                r["trigger_b"] = fp(mf.iloc[0])
            ms = get(fam, dose, "minus_forged_structure")
            if len(ms):
                r["trigger_b_structure"] = bool(r["trigger_b"] and not fp(ms.iloc[0]))
            tm = get(fam, dose, "timing_minus_forged")
            if fam.startswith("T1-") and len(tm):
                r["trigger_b_timing"] = fp(tm.iloc[0])
        if fam == "A1":
            r["a1_gate"] = bool(full.boot_lo > 0.55)
        out.append(r)
    return pd.DataFrame(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for k in ("gen", "sealed", "dataset", "tasks", "answers", "out"):
        ap.add_argument(f"--{k}", type=Path, required=True)
    ap.add_argument("--prereg", required=True, help="Msc-project commit of the pre-registration")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--cases", nargs="*", help="restrict to these case ids (smoke runs only)")
    a = ap.parse_args(argv)
    dev = {json.loads(l)["source"].split("/")[0] for l in a.answers.read_text().splitlines()}
    cases = load_dev_cases(dataset_root=a.dataset, tasks_root=a.tasks, answers_jsonl=a.answers,
                           dev_templates=dev)
    if a.cases:
        cases = [c for c in cases if c.case_id in set(a.cases)]
    a.out.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(a.workers) as pool:
        res = list(pool.map(one_case, [(c, a.gen, a.sealed) for c in cases]))
    pd.DataFrame([r for _, rc in res for r in rc]).to_csv(a.out / "recovery.csv", index=False)
    data = pd.concat([f for fr, _ in res for f in fr], ignore_index=True)
    rows, by_target = evaluate(data)
    auc = pd.DataFrame(rows)
    auc.to_csv(a.out / "auc.csv", index=False)
    pd.DataFrame(by_target).to_csv(a.out / "auc_by_target.csv", index=False)
    v = verdicts(auc)
    v.to_csv(a.out / "verdicts.csv", index=False)
    code = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                          cwd=Path(__file__).resolve().parents[3]).stdout.strip()
    (a.out / "run.env.json").write_text(json.dumps({
        "prereg_commit_msc_project": a.prereg, "code_commit": code, "seed": SEED,
        "max_per_class_per_case": MAX_PER_CLASS_PER_CASE, "bootstrap": f"{BOOT} case resamples",
        "split": "GroupKFold(4) by development template",
        "classifier": "sklearn HistGradientBoostingClassifier(max_iter=200), categoricals "
                      "factorised per family x dose", "forged_fields": FORGED,
        "log_forged": LOG_FORGED, "structure": STRUCT, "timing": TIMING,
        "declared": sorted(DECLARED), "undeclared": sorted(UNDECLARED)}, indent=2))
    print(v.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
