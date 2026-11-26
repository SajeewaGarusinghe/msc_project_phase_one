"""Record-level distinguishability of FrameBench span overlays (K22; proposal §06's trigger).

A port of G0 step 9a (``g0_pilot/stage2/distinguishability.py``,
``Msc-project/09-results/g0/09a-distinguishability.md``) to FrameBench's sealed labels, kept to
its method and its four corrections:

1. negatives are genuine records from the pool the overlay drew from: the target's *server*
   spans after the fault, in the clean twin (never its client spans);
2. categoricals are factorised once over the whole data set;
3. trace size is computed over the whole case, never over the subset being featurised;
4. forged and genuine records are sampled to the same size **within each case**;
5. (new here) the genuine pool stops at the standard window's end, as the overlay does: G0's
   overlays wrote to the whole post-fault period, FrameBench's only inside the window, and an
   open-ended pool let ``offset_s`` separate the classes (F2 0.60-0.64 with the status removed).

Positives are the forged claims named by the sealed labels (``escape != "witness"``) rather
than recovered by diffing; a test checks they are exactly the edited spans. Folds are grouped by
development template. Development data only.

Step 4b adds structural features (parent in file, root, number of children, method equal to the
parent's, status inside gRPC's 0-16), because the adaptive routes change structure, and reports
three feature sets: **full** (the primary measure, pre-registered in the decisions log, 26 Nov),
**minus_status** (status-derived features removed) and **minus_forged** (every feature derived
from a field the family writes removed), which attribute the cause. It also reports two tells the
agent's own tools can show: the caller->target edge count ``call_graph`` reports, and the
genuine caller client spans left without a server child.
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import GroupKFold

from provrca_bench.framebench.case import load_dev_cases

SEED = 20261109                       # G0 9a's seed, reused
MAX_PER_CLASS_PER_CASE = 5000
CATEGORICAL = ["service", "method", "operation", "parent_service", "parent_method"]
STATUS = ["status_code", "code_in_grpc_range"]
DURATION = ["duration_us", "log_duration"]
STRUCTURE = ["parent_service", "parent_method", "is_server", "parent_in_file", "is_root",
             "method_eq_parent"]
FORGED = {"F2": STATUS, "F2cD": STATUS, "FS": STATUS + DURATION,
          "F2c": STATUS + STRUCTURE, "FSc": STATUS + DURATION + STRUCTURE}
FAMILIES = tuple(FORGED)
USECOLS = ["traceID", "spanID", "serviceName", "methodName", "operationName", "startTime",
           "duration", "statusCode", "parentSpanID"]


def read(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, usecols=USECOLS, dtype={"spanID": str, "parentSpanID": str,
                                                     "traceID": str, "methodName": str},
                       keep_default_na=True, low_memory=False)


def features(df: pd.DataFrame, rows: pd.Index, inject: int) -> pd.DataFrame:
    """Features of ``df.loc[rows]``, with every context feature taken from the whole case."""
    sid = df.spanID.astype(str)
    svc_of = dict(zip(sid, df.serviceName.astype(str)))
    meth_of = dict(zip(sid, df.methodName.fillna("").astype(str)))
    kids = df.parentSpanID.dropna().astype(str).value_counts()
    tsize = df.traceID.astype(str).value_counts()
    x = df.loc[rows]
    pid = x.parentSpanID.fillna("").astype(str)
    p_svc = pid.map(svc_of)
    meth = x.methodName.fillna("").astype(str)
    p_meth = pid.map(meth_of)
    code = pd.to_numeric(x.statusCode, errors="coerce")
    dur = pd.to_numeric(x.duration, errors="coerce")
    return pd.DataFrame({
        "duration_us": dur.values,
        "log_duration": np.log10(dur.clip(lower=1)).values,
        "status_code": code.fillna(-1).values,
        "code_in_grpc_range": code.fillna(0).between(0, 16).astype(int).values,
        "offset_s": (pd.to_numeric(x.startTime, errors="coerce") / 1e6 - inject).values,
        "service": x.serviceName.astype(str).values,
        "method": meth.values,
        "operation": x.operationName.astype(str).values,
        "parent_service": p_svc.fillna("<none>").values,
        "parent_method": p_meth.fillna("<none>").values,
        "is_server": (p_svc.notna() & (p_svc != x.serviceName.astype(str))).astype(int).values,
        "parent_in_file": p_svc.notna().astype(int).values,
        "is_root": (pid == "").astype(int).values,
        "method_eq_parent": (p_meth == meth).astype(int).values,
        "n_children": x.spanID.astype(str).map(kids).fillna(0).values,
        "trace_size": x.traceID.astype(str).map(tsize).values,
    })


def _edges(df: pd.DataFrame, lo: int, hi: int, callee: str) -> tuple[int, int]:
    """(caller->callee edges as call_graph counts them in the window, caller client spans that
    name the callee's methods but have no child)."""
    w = df[(df.startTime >= lo) & (df.startTime < hi)]
    par = w[["spanID", "serviceName"]].rename(columns={"spanID": "parentSpanID",
                                                        "serviceName": "caller"})
    e = w.merge(par, on="parentSpanID")
    n_edges = int(((e.serviceName == callee) & (e.caller != callee)).sum())
    svc_of = dict(zip(df.spanID, df.serviceName))
    p_svc = df.parentSpanID.map(svc_of)
    served = (df.serviceName == callee) & p_svc.notna() & (p_svc != callee)
    methods = set(df.loc[served, "methodName"].dropna())       # its RPCs, not its health checks
    has_kid = set(df.parentSpanID.dropna())
    cli = w[(w.serviceName != callee) & w.methodName.isin(methods) & ~w.spanID.isin(has_kid)]
    return n_edges, int(len(cli))


def one_case(args) -> tuple[list[dict], list[dict], list[dict]]:
    case, gen, sealed, families = args
    rng = np.random.default_rng([SEED, int(case.case_id.split("-")[-1])])
    inject = case.inject_time
    lo, hi = inject * 1_000_000, case.window[1] * 1_000_000
    clean = read(gen / case.case_id / "clean" / "traces.csv")
    c_svc = dict(zip(clean.spanID, clean.serviceName))
    frames, recov, tells = [], [], []
    base_edges = None
    for v in sorted(p.name for p in (gen / case.case_id).iterdir()):
        fam, threat, dose = (v.split("-") + ["", ""])[:3]
        if fam not in families or threat != "T2":
            continue
        labels = [json.loads(l) for l in (sealed / case.case_id / f"{v}.labels.jsonl")
                  .read_text().splitlines()]
        target = labels[0]["target"] if labels else None
        ids = {l["record_id"][5:] for l in labels
               if l["record_id"].startswith("span:") and l["escape"] != "witness"}
        ov = read(gen / case.case_id / v / "traces.csv")
        pos = ov.index[ov.spanID.isin(ids)]
        changed = ov.loc[pos]
        c = clean.set_index("spanID").loc[changed.spanID]
        n_changed = int(((c.statusCode.fillna(0).values != changed.statusCode.fillna(0).values)
                         | (c.duration.values != changed.duration.values)
                         | (c.parentSpanID.values != changed.parentSpanID.values)).sum())
        p_svc = clean.parentSpanID.map(c_svc)
        pool = clean.index[(clean.serviceName == target) & p_svc.notna()
                           & (p_svc != clean.serviceName) & (clean.startTime >= lo)
                           & (clean.startTime < hi)
                           & ~clean.spanID.isin(ids)]
        n = min(len(pos), len(pool), MAX_PER_CLASS_PER_CASE)
        recov.append({"case_id": case.case_id, "variant": v, "family": fam, "dose": dose,
                      "target": target, "labelled": len(ids), "found": len(pos),
                      "changed": n_changed, "pool": len(pool), "used": n})
        if base_edges is None:
            base_edges = _edges(clean, lo, hi, target)
        oe = _edges(ov, lo, hi, target)
        tells.append({"case_id": case.case_id, "variant": v, "family": fam, "dose": dose,
                      "target": target, "edges_clean": base_edges[0], "edges_overlay": oe[0],
                      "unpaired_callers_clean": base_edges[1], "unpaired_callers_overlay": oe[1]})
        if n == 0:
            continue
        pos = pos[rng.choice(len(pos), n, replace=False)] if len(pos) > n else pos
        pool = pool[rng.choice(len(pool), n, replace=False)] if len(pool) > n else pool
        X = pd.concat([features(ov, pos, inject), features(clean, pool, inject)],
                      ignore_index=True)
        X["_y"] = [1] * len(pos) + [0] * len(pool)
        X["_template"] = case.template
        X["_family"] = fam
        X["_dose"] = dose
        frames.append(X)
    return frames, recov, tells


def evaluate(data: pd.DataFrame, folds: int = 4) -> list[dict]:
    for col in CATEGORICAL:
        data[col] = pd.factorize(data[col].astype(str))[0]
    rows = []
    for (fam, dose), df in data.groupby(["_family", "_dose"]):
        for mode in ("full", "minus_status", "minus_forged"):
            drop = {"full": [], "minus_status": STATUS, "minus_forged": FORGED[fam]}[mode]
            X = df.drop(columns=["_y", "_template", "_family", "_dose"] + drop)
            y, groups = df["_y"].to_numpy(), df["_template"].to_numpy()
            aucs = []
            for tr, te in GroupKFold(n_splits=folds).split(X, y, groups):
                if len(set(y[te])) < 2:
                    continue
                clf = HistGradientBoostingClassifier(random_state=SEED, max_iter=200)
                clf.fit(X.iloc[tr], y[tr])
                aucs.append(roc_auc_score(y[te], clf.predict_proba(X.iloc[te])[:, 1]))
            rows.append({"family": fam, "dose": dose, "features": mode,
                         "n_forged": int((y == 1).sum()), "n_genuine": int((y == 0).sum()),
                         "n_features": X.shape[1], "folds": len(aucs),
                         "auc_mean": round(float(np.mean(aucs)), 4),
                         "auc_min": round(float(np.min(aucs)), 4),
                         "auc_max": round(float(np.max(aucs)), 4), "dropped": "|".join(drop)})
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gen", type=Path, required=True)
    ap.add_argument("--sealed", type=Path, required=True)
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--tasks", type=Path, required=True)
    ap.add_argument("--answers", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--families", nargs="*", default=list(FAMILIES))
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args(argv)
    dev = {json.loads(l)["source"].split("/")[0] for l in a.answers.read_text().splitlines()}
    cases = load_dev_cases(dataset_root=a.dataset, tasks_root=a.tasks, answers_jsonl=a.answers,
                           dev_templates=dev)
    a.out.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(a.workers) as pool:
        res = list(pool.map(one_case, [(c, a.gen, a.sealed, set(a.families)) for c in cases]))
    pd.DataFrame([r for _, rc, _ in res for r in rc]).to_csv(a.out / "recovery.csv", index=False)
    pd.DataFrame([r for _, _, t in res for r in t]).to_csv(a.out / "tells.csv", index=False)
    data = pd.concat([f for fr, _, _ in res for f in fr], ignore_index=True)
    auc = pd.DataFrame(evaluate(data))
    auc.to_csv(a.out / "auc.csv", index=False)
    (a.out / "run.env.json").write_text(json.dumps({
        "seed": SEED, "max_per_class_per_case": MAX_PER_CLASS_PER_CASE,
        "balanced_within_case": True, "split": "GroupKFold(4) by development template",
        "classifier": "sklearn HistGradientBoostingClassifier(max_iter=200)",
        "families": sorted(a.families), "forged_fields": FORGED,
        "primary": "full (pre-registered, decisions log 2026-11-26)"}, indent=2))
    print(auc.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
