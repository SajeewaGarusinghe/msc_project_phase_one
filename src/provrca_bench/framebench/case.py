"""One clean development case, read once, with the facts every later step needs.

Names differ between files, and the differences are not cosmetic: the task's candidate list and
the metrics say ``frontend``, the traces say ``frontendservice``, and ``frontend-external`` is a
metrics-only pseudo-service with no spans or logs. ``trace_name`` and ``log_name`` are the only
places that mapping lives.

A *server span* is a span whose parent belongs to another service. RCAEval records no span kind,
so this is the inference G0 step 2 validated (98.67% strong on Online Boutique); it is what the
pilot and step 8 used too.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import numpy as np
import pandas as pd

TRACE_ALIAS = {"frontend": "frontendservice", "frontend-external": "frontendservice"}

#: gRPC service in a client span's ``operationName`` -> the trace name of the service it calls
#: (Online Boutique v0.9.0 ``demo.proto``). Used only by ``call_edges``. An unmapped name is dropped
#: and counted, never guessed; health checks are not calls between services.
PEER_OF = {f"hipstershop.{k}Service": f"{k.lower()}service" for k in
           ("Ad", "Cart", "Checkout", "Currency", "Email", "Payment", "ProductCatalog",
            "Recommendation", "Shipping")}
NOT_A_PEER = {"grpc.health.v1.Health", "grpc.grpc.health.v1.Health"}


def trace_name(service: str) -> str:
    return TRACE_ALIAS.get(service, service)


def log_name(service: str) -> str:
    """Containers are named like candidates: ``frontend``, ``currencyservice``."""
    return "frontend" if service in ("frontendservice", "frontend-external") else service


@dataclass
class CleanCase:
    """A rebuilt clean case directory plus its blind task file and its answer (scorer-side)."""
    case_id: str
    case_dir: Path
    task: dict
    root_cause: str                       # trace name; scorer-side, never written to the overlay
    fault: str
    template: str
    _cache: dict = field(default_factory=dict, repr=False)

    @property
    def inject_time(self) -> int:
        return int(self.task["inject_time"])

    @property
    def window(self) -> tuple[int, int]:
        w = self.task["standard_window"]
        return int(w["start"]), int(w["end"])

    @property
    def candidates(self) -> list[str]:
        """Candidate services as trace names, with the metrics-only pseudo-service removed."""
        return sorted({trace_name(c) for c in self.task["candidate_services"]
                       if c != "frontend-external"})

    @cached_property
    def spans(self) -> pd.DataFrame:
        t = pd.read_csv(self.case_dir / "traces.csv",
                        usecols=["spanID", "serviceName", "parentSpanID", "startTime",
                                 "duration", "statusCode"],
                        dtype={"spanID": str, "parentSpanID": str, "serviceName": str},
                        low_memory=False)
        parent_svc = t.parentSpanID.map(dict(zip(t.spanID, t.serviceName)))
        t["parent_service"] = parent_svc
        t["is_server"] = parent_svc.notna() & (parent_svc != t.serviceName)
        t["post"] = (t.startTime // 1_000_000) >= self.inject_time
        return t

    @cached_property
    def edges(self) -> set[tuple[str, str]]:
        """(caller service, callee service) for every cross-service parent link."""
        s = self.spans[self.spans.is_server]
        return set(zip(s.parent_service, s.serviceName))

    @cached_property
    def call_edges(self) -> tuple[set[tuple[str, str]], dict]:
        """(caller, callee) from server spans **and** from client spans that name their peer in
        ``operationName`` (``hipstershop.CartService/GetCart``), so services that emit no spans
        (cart, shipping, ad) keep their edges. ``edges`` is deliberately left as it was: the target
        rule's ``shape`` and AF2's neighbour pool read it, and must not change. Returns the edges and
        a report: how many client-span edges were added and which operation names were unmapped
        (Train Ticket's client spans say only ``GET``, so its graph is server-span-only).
        Distance classification for the distant-target subset reads this."""
        t = pd.read_csv(self.case_dir / "traces.csv", usecols=["serviceName", "operationName"],
                        dtype=str, low_memory=False)
        out = set(self.edges)
        unmapped: dict[str, int] = {}
        added = 0
        ops = t.operationName.fillna("").str.lstrip("/")
        pairs = pd.DataFrame({"svc": t.serviceName, "op": ops}).drop_duplicates()
        for svc, op in zip(pairs.svc, pairs.op):
            if "/" not in op:
                continue
            grpc = op.split("/", 1)[0]
            grpc = grpc[5:] if grpc.startswith("grpc.") and grpc[5:] in PEER_OF else grpc
            if grpc in NOT_A_PEER:
                continue
            peer = PEER_OF.get(grpc)
            if peer is None:
                if "." in grpc:          # a dotted gRPC name we do not know: report, do not guess
                    unmapped[grpc] = unmapped.get(grpc, 0) + 1
                continue
            if peer != svc and (svc, peer) not in out:
                out.add((svc, peer))
                added += 1
        return out, {"client_edges_added": added, "unmapped_grpc_names": unmapped,
                     "peer_source": "server+client" if added else "server_only"}

    def callers_of(self, svc: str) -> set[str]:
        return {a for a, b in self.edges if b == svc}

    def callees_of(self, svc: str) -> set[str]:
        return {b for a, b in self.edges if a == svc}

    def ancestors_of(self, svc: str) -> set[str]:
        """Every transitive caller of ``svc`` in ``edges`` (the graph check (c) reads)."""
        out, stack = set(), list(self.callers_of(svc))
        while stack:
            u = stack.pop()
            if u not in out:
                out.add(u)
                stack.extend(self.callers_of(u))
        return out

    @cached_property
    def log_containers(self) -> set[str]:
        return set(pd.read_csv(self.case_dir / "logs.csv", usecols=["container_name"])
                   .container_name.astype(str).unique())

    def server_spans(self, svc: str, *, post: bool | None = None) -> pd.DataFrame:
        s = self.spans[self.spans.is_server & (self.spans.serviceName == svc)]
        return s if post is None else s[s.post == post]

    def p95_ratio(self, svc: str, min_spans: int = 20) -> float | None:
        """Post-fault over pre-fault p95 server-span duration inside the task's standard window
        (the agent's view, and where overlays write), or None with too few spans."""
        lo, hi = (t * 1_000_000 for t in self.window)
        s = self.server_spans(svc)
        s = s[(s.startTime >= lo) & (s.startTime < hi)]
        pre, post = s[~s.post], s[s.post]
        if len(pre) < min_spans or len(post) < min_spans:
            return None
        return float(np.percentile(post.duration, 95) / max(np.percentile(pre.duration, 95), 1))


def load_dev_cases(*, dataset_root: Path, tasks_root: Path, answers_jsonl: Path,
                   dev_templates: set[str]) -> list[CleanCase]:
    """Every development case, refusing any template outside the development split before it
    opens anything (the hygiene rule in force after K10)."""
    out = []
    for line in Path(answers_jsonl).read_text().splitlines():
        a = json.loads(line)
        template, rep = a["source"].split("/")
        if template not in dev_templates:
            raise PermissionError(f"{a['case_id']} ({template}) is not a development template")
        task = json.loads((Path(tasks_root) / a["case_id"] / "task.json").read_text())
        out.append(CleanCase(case_id=a["case_id"], case_dir=Path(dataset_root) / template / rep,
                             task=task, root_cause=trace_name(a["service"]), fault=a["fault"],
                             template=template))
    return sorted(out, key=lambda c: c.case_id)
