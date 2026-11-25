"""Demotion at the tool layer (build guide P3 item 6; proposal §08 "Verdicts act at the tool layer",
Fig 13; §14's silent-withholding check).

**M2 (withhold, primary).** Withheld records are left out of a *view* of the case, and RCAEval's
own ``CaseToolkit`` runs on that view, so every aggregate (trace_summary's p50/p90/errors,
search_logs' groups, the ``tracets_*``/``logts`` series behind rank_anomalies, call_graph) is
recomputed by the same code the undefended agent uses. The run's toolkit and MCP server point at
the view; the raw case is never opened in a withholding run. The layer's ``filter`` then adds a
note saying what was withheld in the call's scope. **M2-silent** is the same view with no note
(§14). **M1 (annotate, ablation)** leaves the raw case in place and tags each output row with the
verdict counts of the records behind it.

**What is withheld is the arm's choice, not the code's:** D1 (sender authentication) withholds
``rejected``; D4 (ProvRCA) withholds ``rejected`` and ``contradicted``. ``build_view`` takes a
generic drop mask, so the P4 comparators (D2, D3) can share this recomputation path and note
template, and no arm differs from another in how its output is produced.

The view is built from a task view (its directory name carries no family or dose, A4), and its own
directory is named by a hash of the input and the drop set, so identical drop sets are built once.
With nothing to drop, the view *is* the input view.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from provrca_bench.defence import DefenceLayer
from provrca_bench.provrca.chain import judge_logs, judge_spans
from provrca_bench.provrca.rules import Config
from provrca_bench.verdicts import Reason, Verdict

#: what each arm withholds (proposal §10 arms table)
DEMOTES = {"D1": frozenset({Verdict.REJECTED.value}),
           "D4": frozenset({Verdict.REJECTED.value, Verdict.CONTRADICTED.value})}

MODES = ("M2", "M2-silent", "M1")

#: RCAEval's export mapping: task-view name -> case-directory name (``RCAEval/agent/export.py``)
DERIVED = {"logts.csv": "logts.csv", "tracets_err.csv": "tracets_err.csv",
           "tracets_lat.csv": "tracets_lat.csv", "log_templates.json": "cluster_info.json"}

WHY = {
    Reason.R1_ENCLOSURE_FAIL.value: "duration exceeds the caller's wait (R1)",
    Reason.R2_STATUS_CONFLICT.value: "status code conflicts with the code the caller received (R2)",
    Reason.ATTR_MISMATCH.value: "sender does not match the claimed service",
}
MAX_NOTE_LINES = 12


# ------------------------------------------------------------------ records of a file
def split_records(path: Path) -> tuple[str, list[str]]:
    """Header and raw records of a CSV, each exactly as written (quoted newlines included).

    A record ends where the running count of '"' is even: inside a quoted field quotes are doubled,
    so parity is exact for RFC 4180 CSV. Record i is pandas row i of the same file.
    """
    header, records, buf, quotes = None, [], [], 0
    with open(path, "r", encoding="utf-8", newline="") as f:
        for line in f:
            buf.append(line)
            quotes += line.count('"')
            if quotes % 2 == 0:
                rec = "".join(buf)
                if header is None:
                    header = rec
                else:
                    records.append(rec)
                buf, quotes = [], 0
    if buf:
        raise ValueError(f"{path}: unterminated quoted field at end of file")
    return header or "", records


def write_filtered(src: Path, dst: Path, drop: np.ndarray) -> int:
    header, records = split_records(src)
    if len(records) != len(drop):
        raise ValueError(f"{src}: {len(records)} records but a drop mask of {len(drop)}")
    with open(dst, "w", encoding="utf-8", newline="") as f:
        f.write(header)
        for rec, d in zip(records, drop):
            if not d:
                f.write(rec)
    return int(drop.sum())


# ------------------------------------------------------------------ the view
def view_key(task_view: Path, drop_spans: np.ndarray, drop_logs: np.ndarray) -> str:
    h = hashlib.sha256((task_view / "task.json").read_bytes())
    for name in ("traces.csv", "logs.csv"):
        p = task_view / name
        h.update(f"{name}:{p.stat().st_size}:{p.stat().st_ino}".encode())
    h.update(np.flatnonzero(drop_spans).astype(np.int64).tobytes())
    h.update(b"|")
    h.update(np.flatnonzero(drop_logs).astype(np.int64).tobytes())
    return h.hexdigest()


def _derive(case_dir: Path, inject_time: int, python: str) -> None:
    r = subprocess.run([python, "-m", "RCAEval.convert", "derive", str(case_dir),
                        "--inject-time", str(inject_time)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"derive failed for {case_dir}: {r.stderr.strip()[-600:]}")


def build_view(task_view: Path, out_root: Path, *, drop_spans: np.ndarray, drop_logs: np.ndarray,
               python: str, force: bool = False) -> Path:
    """The task view with the dropped records left out and the derived files rebuilt.

    ``task.json`` and the metrics files are the input's own bytes (metrics are written by Envoy
    and cAdvisor, never filtered, K23); traces and logs keep every other record byte for byte;
    ``tracets_*``, ``logts`` and the log templates are rebuilt by the same ``derive`` call used for
    every clean and overlay case (A3). Removing log lines can change Drain's templates and so the
    ``logts`` column ids: that is the recomputation, not a defect.
    """
    task_view = Path(task_view)
    if not force and not drop_spans.any() and not drop_logs.any():
        return task_view
    out = Path(out_root) / view_key(task_view, drop_spans, drop_logs)
    if (out / ".complete").exists():
        return out
    task_bytes = (task_view / "task.json").read_bytes()
    task = json.loads(task_bytes)
    files = task["files"]
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    with tempfile.TemporaryDirectory(dir=out_root) as tmp:
        tmp = Path(tmp)
        write_filtered(task_view / files["traces"], tmp / "traces.csv", drop_spans)
        write_filtered(task_view / files["logs"], tmp / "logs.csv", drop_logs)
        _derive(tmp, int(task["inject_time"]), python)
        (out / "task.json").write_bytes(task_bytes)
        for key, name in files.items():
            src_case = DERIVED.get(name)
            if name in ("traces.csv", "logs.csv"):
                shutil.move(tmp / name, out / name)
            elif src_case is not None:
                shutil.copyfile(tmp / src_case, out / name)
            else:                                   # metrics, metrics_raw: never filtered
                os.link(task_view / name, out / name)
    (out / ".complete").write_text("")
    return out


# ------------------------------------------------------------------ verdicts of one case
def case_records(task_view: Path, cfg: Config, *, attribution: bool = True) -> pd.DataFrame:
    """Every evidence item of a task view with its verdict and the fields the tools scope by.

    Rows are traces.csv's records then logs.csv's, in file order, so a ``drop`` mask lines up with
    ``split_records``. ``t``/``service``/``operation``/``pattern`` are computed with the toolkit's
    own helpers, so a note's scope is exactly the call's scope.
    """
    from RCAEval.agent.toolkit import mask_message, to_seconds

    tv = Path(task_view)
    task = json.loads((tv / "task.json").read_text())
    tr = pd.read_csv(tv / task["files"]["traces"], dtype=str, keep_default_na=False, low_memory=False)
    js = judge_spans(tr, cfg, attribution=attribution)
    spans = js.assign(t=to_seconds(tr["startTime"]).values, service=tr["serviceName"].values,
                      operation=tr.get("operationName", "").values if "operationName" in tr else "",
                      message="", pattern="")
    lg = pd.read_csv(tv / task["files"]["logs"], dtype=str, keep_default_na=False)
    jl = judge_logs(lg, attribution=attribution)
    logs = jl.assign(t=to_seconds(lg["timestamp"]).values, service=lg["container_name"].values,
                     operation="", message=lg["message"].values,
                     pattern=lg["message"].map(mask_message).values)
    return pd.concat([spans, logs], ignore_index=True)


def drop_masks(records: pd.DataFrame, demote: Iterable[str]) -> tuple[np.ndarray, np.ndarray]:
    w = records.verdict.isin(set(demote)).to_numpy()
    k = records.kind.to_numpy()
    return w[k == "span"], w[k == "log"]


# ------------------------------------------------------------------ the layer
def _label(verdict: str, reason: str, detail: str) -> str:
    if verdict == Verdict.CONTRADICTED.value:
        return f"contradicted: {WHY[reason]}"
    if verdict == Verdict.REJECTED.value:
        return f"rejected: {WHY[reason]}"
    if verdict == Verdict.COMPATIBLE.value:
        return "compatible: a rule applied and held (not verified)"
    return f"unknown: {reason}{(' ' + detail) if detail else ''}"


class ProvRCALayer(DefenceLayer):
    """D1 or D4 in mode M2, M2-silent or M1, for one case.

    In M2 and M2-silent the run's toolkit must be built on ``view`` (``build_view``); in M1 on the
    raw task view. ``filter`` never reads ``sender``: verdicts were formed when the case was judged.
    """

    def __init__(self, *, arm: str, mode: str, records: pd.DataFrame, task: dict) -> None:
        if arm not in DEMOTES or mode not in MODES:
            raise ValueError(f"unknown arm {arm!r} or mode {mode!r}")
        self.arm, self.mode, self.task = arm, mode, task
        self.records = records
        self.demote = DEMOTES[arm]
        self.withheld = records[records.verdict.isin(self.demote)]

    def _window(self, args: dict) -> tuple[float, float]:
        w = self.task["standard_window"]
        s, e = args.get("start"), args.get("end")
        return (w["start"] if s is None else int(float(s)), w["end"] if e is None else int(float(e)))

    def _scope(self, frame: pd.DataFrame, tool: str, args: dict) -> pd.DataFrame | None:
        """The records a call reads, or None for tools that read no span or log."""
        if tool == "trace_summary":
            s, e = self._window(args)
            f = frame[(frame.kind == "span") & (frame.t >= s) & (frame.t < e)]
            if args.get("service"):
                f = f[f.service.str.lower() == str(args["service"]).strip().lower()]
            return f
        if tool == "search_logs":
            s, e = self._window(args)
            f = frame[(frame.kind == "log") & (frame.t >= s) & (frame.t < e)]
            if args.get("service"):
                f = f[f.service.str.lower() == str(args["service"]).strip().lower()]
            if args.get("pattern"):
                f = f[f.message.str.contains(args["pattern"], case=False, regex=True)]
            return f
        w = self.task["standard_window"]
        in_window = (frame.t >= w["start"]) & (frame.t < w["end"])
        if tool == "call_graph" or (tool == "rank_anomalies"
                                    and args.get("modality") in ("trace_latency", "trace_errors")):
            return frame[(frame.kind == "span") & in_window]
        if tool == "rank_anomalies" and args.get("modality") == "logs":
            return frame[(frame.kind == "log") & in_window]
        return None          # metrics tools, describe_case, submit: nothing withheld can reach them

    def _note(self, tool: str, args: dict) -> list[str]:
        f = self._scope(self.withheld, tool, args)
        if f is None or f.empty:
            return []
        g = f.groupby(["service", "kind", "reason"]).size().sort_values(ascending=False)
        lines = [f"{n} {'spans' if k == 'span' else 'log lines'} from {svc} withheld: {WHY[r]}"
                 for (svc, k, r), n in g.items()]
        if len(lines) > MAX_NOTE_LINES:
            rest = int(g.iloc[MAX_NOTE_LINES - 1:].sum())
            lines = lines[:MAX_NOTE_LINES - 1] + [f"{rest} further records withheld"]
        return lines

    def _annotate(self, tool: str, args: dict, out: dict) -> dict:
        f = self._scope(self.records, tool, args)
        if f is None or f.empty:
            return out
        out = dict(out)

        def counts(sub: pd.DataFrame) -> dict[str, int]:
            c = sub.groupby(["verdict", "reason", "detail"]).size()
            res: dict[str, int] = {}
            for (v, r, d), n in c.items():
                key = _label(v, r, d)
                res[key] = res.get(key, 0) + int(n)
            return dict(sorted(res.items(), key=lambda kv: -kv[1]))

        if tool == "trace_summary" and "rows" in out:
            by = {k: g for k, g in f.groupby(["service", "operation"])}
            out["rows"] = [dict(r, verdicts=counts(by[(r["service"], r["operation"])]))
                           if (r.get("service"), r.get("operation")) in by else r for r in out["rows"]]
        elif tool == "search_logs" and "groups" in out:
            by = {k: g for k, g in f.groupby(["service", "pattern"])}
            out["groups"] = [dict(g, verdicts=counts(by[(g["service"], g["pattern"])]))
                             if (g.get("service"), g.get("pattern")) in by else g for g in out["groups"]]
        else:
            out["verdicts"] = {svc: counts(g) for svc, g in f.groupby("service")}
        return out

    def filter(self, tool: str, args: dict[str, Any], raw_output: Any,
               sender: str | None) -> tuple[Any, list]:
        if self.mode == "M2-silent" or not isinstance(raw_output, dict) or "error" in raw_output:
            return raw_output, []
        if self.mode == "M2":
            note = self._note(tool, args or {})
            return (dict(raw_output, withheld=note) if note else raw_output), []
        return self._annotate(tool, args or {}, raw_output), []


# ------------------------------------------------------------------ audit
def write_case_audit(run_dir: Path, *, run_id: str, case_id: str, records: pd.DataFrame,
                     arm: str, cfg: Config, view: Path) -> dict:
    """Per (run, case): the full per-item verdict table (compressed CSV), one ``VerdictRecord`` per
    withheld item in ``audit.jsonl``, and a sidecar with the hashes. An amendment to A3's "one line
    per evidence item" (decisions log, 25 Nov, P3 step 3): every item is still recorded, once."""
    from provrca_bench.contracts import VerdictRecord
    from provrca_bench.defence import AuditLog

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    table = run_dir / f"verdicts-{case_id}.csv.gz"
    records.drop(columns=["message"]).to_csv(table, index=False, compression={"method": "gzip", "mtime": 0})
    withheld = records[records.verdict.isin(DEMOTES[arm])]
    recs = []
    for r in withheld.itertuples():
        inputs = {"kind": r.kind, "service": r.service, "detail": r.detail or None}
        if r.kind == "span":
            inputs.update(route=r.route or None, tier=r.tier or None, r1=r.r1_reason or None,
                          r2=r.r2_reason or None, server_us=r.server_us or None,
                          client_us=r.client_us or None, server_code=r.server_code or None,
                          client_code=r.client_code or None, epsilon_us=cfg.epsilon_us)
        inputs["table_sha256"] = cfg.sha256
        recs.append(VerdictRecord.of(run_id=run_id, case_id=case_id, record_id=r.record_id,
                                     reason=Reason(r.reason), rule_inputs=inputs,
                                     note=f"withheld: {WHY[r.reason]}"))
    AuditLog(run_dir / "audit.jsonl").write(recs)
    meta = {"case_id": case_id, "arm": arm, "items": len(records), "withheld": len(withheld),
            "table_sha256": cfg.sha256,
            "verdict_table_sha256": hashlib.sha256(table.read_bytes()).hexdigest(),
            "view": view.name}
    (run_dir / f"audit-{case_id}.meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True))
    return meta


__all__ = ["DEMOTES", "MODES", "ProvRCALayer", "build_view", "case_records", "drop_masks",
           "split_records", "write_case_audit", "write_filtered"]
