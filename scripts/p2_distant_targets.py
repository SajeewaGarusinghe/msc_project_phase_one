"""The distant-target subset's rule, run on the development cases and on RE3-TT (Train Ticket's
calibration set). No test case is read.

For every case: each candidate's distance class and hops in the full call graph, the main rule's
checks, and whether it is an eligible distant target; the graph's edge report (client-span edges
added, unmapped gRPC names, peer source); and the seeded draw. A per-stratum seeded 10% subset is
selected among the cases with an eligible distant candidate, to show the rule end to end.

RE3-TT has no task.json. A minimal task is built: ``inject_time`` from ``inject_time.txt`` and, as
candidates, every service that writes a span or a log line. The root cause is the template
directory's service (``ts-auth-service_f1`` -> ``ts-auth-service``).

    uv run python scripts/p2_distant_targets.py --out <dir>
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

from provrca_bench.framebench.case import CleanCase, load_dev_cases
from provrca_bench.framebench.target import distant_subset, draw_distant_target

SEED = 20261125
REPO = Path(__file__).resolve().parents[1]


def re3tt_cases(root: Path) -> list[CleanCase]:
    out = []
    for tdir in sorted(p for p in root.iterdir() if p.is_dir()):
        svc = tdir.name.split("_")[0]
        for rep in sorted(p for p in tdir.iterdir() if p.is_dir()):
            inj = int((rep / "inject_time.txt").read_text().strip())
            t = pd.read_csv(rep / "traces.csv", usecols=["serviceName"], dtype=str).serviceName
            lg = pd.read_csv(rep / "logs.csv", usecols=["container_name"], dtype=str).container_name
            cands = sorted(set(t.dropna()) | set(lg.dropna()))
            task = {"inject_time": inj, "candidate_services": cands,
                    "standard_window": {"start": inj - 600, "end": inj + 600}}
            out.append(CleanCase(case_id=f"re3-tt/{tdir.name}/{rep.name}", case_dir=rep, task=task,
                                 root_cause=svc, fault=tdir.name.split("_", 1)[1], template=tdir.name))
    return out


def _one(case: CleanCase) -> tuple[list[dict], dict]:
    drawn, rows = draw_distant_target(case, SEED)
    _, report = case.call_edges
    for r in rows:
        r["drawn"] = r["service"] == drawn
    cls = Counter(r["class"] for r in rows)
    return rows, {"case_id": case.case_id, "root_cause": case.root_cause, "drawn": drawn or "",
                  "drawn_hops": next((r["hops"] for r in rows if r["drawn"]), ""),
                  "eligible": sum(r["eligible"] for r in rows),
                  "distant": cls["distant"], "neighbour": cls["neighbour"], "ancestor": cls["ancestor"],
                  "near_other": cls["near_other"], "unknown": cls["unknown"],
                  "edges": len(case.call_edges[0]), **{k: json.dumps(v) if isinstance(v, dict) else v
                                                       for k, v in report.items()}}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--re3tt", type=Path, default=Path("/home/opc/g0_pilot/data/RE3/RE3-TT"))
    p.add_argument("--workers", type=int, default=6)
    a = p.parse_args(argv)
    ans = REPO / "parity/tasks-rebuilt/re2-ob.answers.jsonl"
    dev = {json.loads(l)["source"].split("/")[0] for l in ans.read_text().splitlines()}
    strata = {
        "dev-re2-ob": load_dev_cases(dataset_root=REPO / "parity/dataset-rebuilt",
                                     tasks_root=REPO / "parity/tasks-rebuilt/re2-ob/cases",
                                     answers_jsonl=ans, dev_templates=dev),
        "re3-tt": re3tt_cases(a.re3tt),
    }
    a.out.mkdir(parents=True, exist_ok=True)
    summary = {}
    for name, cases in strata.items():
        with ProcessPoolExecutor(a.workers) as pool:
            res = list(pool.map(_one, cases))
        cand = [r for rows, _ in res for r in rows]
        per_case = [c for _, c in res]
        for fname, rows in ((f"{name}_candidates.csv", cand), (f"{name}_cases.csv", per_case)):
            with (a.out / fname).open("w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
        elig = [c["case_id"] for c in per_case if c["eligible"]]
        subset = distant_subset(elig, SEED)
        summary[name] = {
            "cases": len(per_case), "cases_with_eligible_distant": len(elig),
            "subset_10pct": subset,
            "class_counts": dict(Counter(r["class"] for r in cand)),
            "eligible_by_service": dict(Counter(r["service"] for r in cand if r["eligible"])),
            "distant_failing": dict(Counter(
                ("a" if not r["a_no_alarm"] else "") + ("b" if not r["b_no_error_rise"] else "")
                + ("c" if not r["c_not_upstream_caller"] else "") + ("d" if not r["d_has_spans_and_logs"] else "")
                or "none" for r in cand if r["class"] == "distant")),
            "drawn_hops": dict(Counter(str(c["drawn_hops"]) for c in per_case if c["drawn"])),
            "peer_source": dict(Counter(c["peer_source"] for c in per_case)),
            "unmapped_grpc_names": sorted({k for c in per_case for k in json.loads(c["unmapped_grpc_names"])}),
        }
    (a.out / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
