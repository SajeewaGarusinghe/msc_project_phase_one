"""P3-T3 (Skew) and P3-T4 (Mislabel) on the development set: the honest faults AF1 and AF2.

For every development case with AF variants, the clean variant and each AF variant are judged by
the P3 decision chain with attribution on and off. AF1 and AF2 edit rows in place and never
re-sort a file, so every record is compared with the same row of the clean case.

P3-T3 (AF1, D's clock skewed by +/-250 ms) passes when no record's verdict changes in the decision
chain, in either mode. It holds by construction, because no rule reads a timestamp. Step 5's
review (item 1) asked for the agent-visible effects to be reported too, because time *is* read
outside the chain: D's records that cross an edge of the task's standard window, and the derived
``tracets_*`` files that RCAEval rebuilds from time buckets. The timestamp-enclosure
counterfactual (the rejected alternative to R1) is swept over delta = 1, 10, 100, 250 ms on D's
clean matched pairs.

P3-T4 (AF2, some of B's requests relabelled as D) passes when:
  * with attribution on, every relabelled span is rejected (ATTR_MISMATCH);
  * with attribution off, no relabelled span is contradicted (R1/R2 do not catch it);
  * in both modes, no genuine (unlabelled) record is contradicted (review item 7).
Every change to a genuine record's verdict is counted by (clean -> overlay, reason, detail), and
B's and D's span volumes before and after are reported (review item 5).

Output: one JSON file with per-variant results and a summary.

    PYTHONPATH=src python scripts/p3_honest_faults.py \
        --gen cases/framebench-dev --sealed sealed/framebench-dev \
        --dataset parity/dataset-rebuilt --tasks parity/tasks-rebuilt/re2-ob/cases \
        --answers parity/tasks-rebuilt/re2-ob.answers.jsonl --out evidence/p3_honest_faults.json
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from provrca_bench.framebench import adaptive
from provrca_bench.framebench.case import CleanCase, load_dev_cases, log_name
from provrca_bench.provrca.chain import judge_logs, judge_spans, read_traces
from provrca_bench.provrca.rules import Config

DELTAS_MS = (1, 10, 100, 250)


def _judge(vdir: Path, cfg: Config, attribution: bool) -> tuple[pd.DataFrame, pd.DataFrame]:
    sp = judge_spans(read_traces(vdir / "traces.csv"), cfg, attribution=attribution)
    lg = pd.read_csv(vdir / "logs.csv", dtype=str, keep_default_na=False, low_memory=False)
    return sp, judge_logs(lg, attribution=attribution)


def _changes(clean: pd.DataFrame, over: pd.DataFrame, labelled: np.ndarray) -> dict:
    """Row-wise comparison; ``labelled`` marks the rows the overlay edited."""
    assert len(clean) == len(over) and (clean.record_id.values == over.record_id.values).all()
    diff = ((clean.verdict.values != over.verdict.values) | (clean.reason.values != over.reason.values)
            | (clean.detail.values != over.detail.values))
    g = diff & ~labelled
    keys = Counter(zip(clean.verdict.values[g], over.verdict.values[g], over.reason.values[g],
                       over.detail.values[g]))
    return {
        "records": int(len(clean)), "labelled": int(labelled.sum()),
        "changed_labelled": int((diff & labelled).sum()), "changed_genuine": int(g.sum()),
        "genuine_changes": {f"{a}->{b} {r} {d}".strip(): n for (a, b, r, d, ), n in keys.most_common()},
        "genuine_contradicted": int(((over.verdict.values == "contradicted") & ~labelled).sum()),
        "genuine_new_contradicted": int(((over.verdict.values == "contradicted")
                                         & (clean.verdict.values != "contradicted") & ~labelled).sum()),
        "genuine_new_rejected": int(((over.verdict.values == "rejected")
                                     & (clean.verdict.values != "rejected") & ~labelled).sum()),
        "labelled_verdicts": dict(Counter(over.verdict.values[labelled])),
        "labelled_reasons": dict(Counter(over.reason.values[labelled])),
    }


def _window_crossings(case: CleanCase, vdir: Path, target: str) -> dict:
    lo, hi = case.window
    out = {}
    c = pd.read_csv(case.case_dir / "traces.csv", usecols=["serviceName", "startTime"], low_memory=False)
    o = pd.read_csv(vdir / "traces.csv", usecols=["serviceName", "startTime"], low_memory=False)
    m = (c.serviceName == target).to_numpy()
    cin = ((c.startTime // 1_000_000 >= lo) & (c.startTime // 1_000_000 < hi)).to_numpy()
    oin = ((o.startTime // 1_000_000 >= lo) & (o.startTime // 1_000_000 < hi)).to_numpy()
    out["spans_in_window_clean"] = int((cin & m).sum())
    out["spans_entering"] = int((~cin & oin & m).sum())
    out["spans_leaving"] = int((cin & ~oin & m).sum())
    c = pd.read_csv(case.case_dir / "logs.csv", usecols=["container_name", "timestamp"], low_memory=False)
    o = pd.read_csv(vdir / "logs.csv", usecols=["container_name", "timestamp"], low_memory=False)
    m = (c.container_name.astype(str) == log_name(target)).to_numpy()
    cin = ((c.timestamp // 1_000_000_000 >= lo) & (c.timestamp // 1_000_000_000 < hi)).to_numpy()
    oin = ((o.timestamp // 1_000_000_000 >= lo) & (o.timestamp // 1_000_000_000 < hi)).to_numpy()
    out["logs_in_window_clean"] = int((cin & m).sum())
    out["logs_entering"] = int((~cin & oin & m).sum())
    out["logs_leaving"] = int((cin & ~oin & m).sum())
    return out


def _derived_diff(clean_dir: Path, vdir: Path) -> dict:
    out = {}
    for name in ("tracets_lat.csv", "tracets_err.csv", "logts.csv"):
        a, b = clean_dir / name, vdir / name
        if not (a.exists() and b.exists()):
            continue
        x, y = pd.read_csv(a), pd.read_csv(b)
        if list(x.columns) != list(y.columns) or len(x) != len(y):
            out[name] = {"shape_clean": list(x.shape), "shape_overlay": list(y.shape)}
            continue
        neq = ~((x == y) | (x.isna() & y.isna()))
        out[name] = {"cells": int(x.size), "cells_changed": int(neq.values.sum()),
                     "rows_changed": int(neq.any(axis=1).sum()),
                     "columns_changed": sorted(c for c in x.columns if neq[c].any())}
    return out


def _enclosure_sweep(case: CleanCase, target: str) -> dict:
    """Share of D's clean matched pairs whose server interval leaves its client interval when D's
    side is shifted by +/-delta (D as callee: the server side moves; D as caller: the client)."""
    p = adaptive.clean_pairs(str(case.case_dir))
    caller = p.route.str.split(">").str[0]
    callee = p.route.str.split(">").str[1].str.split("/").str[0]
    out = {"pairs": int(((callee == target) | (caller == target)).sum())}
    for sign in (+1, -1):
        for d in (0, *DELTAS_MS):
            dus = sign * d * 1000
            s0 = p.s_start + np.where(callee == target, dus, 0)
            c0 = p.c_start + np.where(caller == target, dus, 0)
            inside = (s0 >= c0) & (s0 + p.s_dur <= c0 + p.c_dur)
            sel = (callee == target) | (caller == target)
            out[f"{'+' if sign > 0 else '-'}{d}ms"] = int((~inside & sel).sum())
    return out


def _volumes(case_dir: Path, vdir: Path, svcs: list[str]) -> dict:
    c = pd.read_csv(case_dir / "traces.csv", usecols=["serviceName"]).serviceName.value_counts()
    o = pd.read_csv(vdir / "traces.csv", usecols=["serviceName"]).serviceName.value_counts()
    return {s: {"clean": int(c.get(s, 0)), "overlay": int(o.get(s, 0))} for s in svcs}


def _one(args) -> dict:
    case, gen, sealed, variants = args
    cfg = Config.load()
    cdir = Path(gen) / case.case_id / "clean"
    clean = {a: _judge(cdir, cfg, a) for a in (True, False)}
    res = {"case_id": case.case_id, "root_cause": case.root_cause, "variants": {}}
    for v, fam, target in variants:
        vdir = Path(gen) / case.case_id / v
        labs = [json.loads(l) for l in (Path(sealed) / case.case_id / f"{v}.labels.jsonl").open()]
        meta = json.loads((Path(sealed) / case.case_id / f"{v}.meta.json").read_text())
        lab_ids = {l["record_id"] for l in labs}
        r = {"family": fam, "target": target}
        for a in (True, False):
            sp, lg = _judge(vdir, cfg, a)
            csp, clg = clean[a]
            # duplicated span ids: a label names an id, so every row with it counts as labelled,
            # but the builder edits only the rows the target (AF1) or B (AF2) wrote
            r[f"spans_attr_{'on' if a else 'off'}"] = _changes(csp, sp, sp.record_id.isin(lab_ids).to_numpy())
            r[f"logs_attr_{'on' if a else 'off'}"] = _changes(clg, lg, lg.record_id.isin(lab_ids).to_numpy())
        if fam == "AF1":
            r["delta_us"] = meta["af1_delta_us"]
            r["co_located"] = meta["af1_co_located"]
            r["window"] = _window_crossings(case, vdir, target)
            r["derived"] = _derived_diff(cdir, vdir)
        else:
            b = meta["af2_b"]
            r.update(b=b, capped_by_b=meta["af2_capped_by_b"], candidates=meta["af2_candidates"],
                     excluded=meta["af2_excluded"], requested=meta["af2_requested"],
                     requests=meta["af2_requests"], spans=meta["af2_spans"],
                     requests_pre_post=meta["af2_requests_pre_post"])
            r["volumes"] = _volumes(case.case_dir, vdir, [target, b] if b else [target])
            r["derived"] = _derived_diff(cdir, vdir)
            if b:
                on = judge_spans(read_traces(vdir / "traces.csv"), cfg, attribution=False)
                rel = on[on.record_id.isin(lab_ids)]
                r["relabelled_routes_attr_off"] = dict(Counter(rel.route[rel.route != ""]).most_common(10))
        res["variants"][v] = r
    res["enclosure"] = _enclosure_sweep(case, variants[0][2]) if variants else None
    return res


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for k in ("gen", "sealed", "dataset", "tasks", "answers", "out"):
        p.add_argument(f"--{k}", type=Path, required=True)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--cases", nargs="*")
    a = p.parse_args(argv)
    dev = {json.loads(l)["source"].split("/")[0] for l in a.answers.read_text().splitlines()}
    cases = {c.case_id: c for c in load_dev_cases(dataset_root=a.dataset, tasks_root=a.tasks,
                                                   answers_jsonl=a.answers, dev_templates=dev)}
    rows = [r for r in csv.DictReader((a.sealed / "index.csv").open()) if r["family"] in ("AF1", "AF2")]
    by: dict[str, list] = {}
    for r in rows:
        if not a.cases or r["case_id"] in a.cases:
            by.setdefault(r["case_id"], []).append((r["variant"], r["family"], r["target"]))
    jobs = [(cases[c], a.gen, a.sealed, sorted(v)) for c, v in sorted(by.items())]
    with ProcessPoolExecutor(a.workers) as pool:
        results = list(pool.map(_one, jobs))

    t3 = t4 = True
    summ = Counter()
    for res in results:
        for v, r in res["variants"].items():
            for mode in ("on", "off"):
                s, l = r[f"spans_attr_{mode}"], r[f"logs_attr_{mode}"]
                if r["family"] == "AF1":
                    ch = s["changed_labelled"] + s["changed_genuine"] + l["changed_labelled"] + l["changed_genuine"]
                    summ[f"AF1_changed_verdicts_attr_{mode}"] += ch
                    t3 &= ch == 0
                else:
                    summ[f"AF2_genuine_changed_attr_{mode}"] += s["changed_genuine"]
                    summ[f"AF2_genuine_contradicted_attr_{mode}"] += s["genuine_contradicted"]
                    summ[f"AF2_genuine_new_rejected_attr_{mode}"] += s["genuine_new_rejected"]
                    summ[f"AF2_relabelled_attr_{mode}"] += s["labelled"]
                    summ[f"AF2_relabelled_contradicted_attr_{mode}"] += s["labelled_verdicts"].get("contradicted", 0)
                    t4 &= s["genuine_contradicted"] == 0
                    if mode == "on":
                        rej = s["labelled_reasons"].get("ATTR_MISMATCH", 0)
                        summ["AF2_relabelled_rejected_attr_on"] += rej
                        t4 &= rej == s["labelled"]
                    else:
                        t4 &= s["labelled_verdicts"].get("contradicted", 0) == 0
            if r["family"] == "AF1":
                for k in ("spans_entering", "spans_leaving", "logs_entering", "logs_leaving"):
                    summ[f"AF1_{k}"] += r["window"][k]
    out = {"P3_T3_pass": bool(t3), "P3_T4_pass": bool(t4), "summary": dict(summ),
           "cases": len(results), "results": results}
    a.out.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(json.dumps({k: out[k] for k in ("P3_T3_pass", "P3_T4_pass", "cases", "summary")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
