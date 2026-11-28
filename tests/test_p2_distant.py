"""The distant-target subset (proposal section 12): distance classes on the full call graph, the
seeded draw and the 10% selection. Unit tests on synthetic traces; real-data tests on the
development set (no eligible distant target exists there, and the main rule's output is unchanged).
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import pytest

from provrca_bench.framebench.case import CleanCase, load_dev_cases
from provrca_bench.framebench.target import (check_candidates, distance_classes, distant_subset)

REPO = Path(__file__).resolve().parents[1]
SEALED = Path(os.environ.get("PROVRCA_FRAMEBENCH_SEALED", REPO / "sealed" / "framebench-dev"))
DATASET = REPO / "parity" / "dataset-rebuilt"
TASKS = REPO / "parity" / "tasks-rebuilt" / "re2-ob" / "cases"
ANSWERS = REPO / "parity" / "tasks-rebuilt" / "re2-ob.answers.jsonl"

HEADER = "spanID,serviceName,parentSpanID,startTime,duration,statusCode,operationName\n"


def _case(tmp_path: Path, rows: list[tuple], root: str, cands: list[str]) -> CleanCase:
    """rows: (spanID, service, parent, operationName)."""
    d = tmp_path / root
    d.mkdir()
    body = "".join(f"{s},{svc},{par},1000000,10,0,{op}\n" for s, svc, par, op in rows)
    (d / "traces.csv").write_text(HEADER + body)
    return CleanCase(case_id="syn", case_dir=d, task={"inject_time": 0, "candidate_services": cands},
                     root_cause=root, fault="cpu", template="x")


def _boutique(tmp_path, root):
    """Online Boutique's shape: frontend calls checkout, currency, recommendation and (span-less)
    ad; checkout calls email, payment, productcatalog; recommendation calls productcatalog."""
    rows = [("f", "frontendservice", "", "/"),
            ("c", "checkoutservice", "f", "hipstershop.CheckoutService/PlaceOrder"),
            ("cu", "currencyservice", "f", "hipstershop.CurrencyService/Convert"),
            ("r", "recommendationservice", "f", "hipstershop.RecommendationService/List"),
            ("fa", "frontendservice", "f", "hipstershop.AdService/GetAds"),   # client span, no server
            ("e", "emailservice", "c", "hipstershop.EmailService/Send"),
            ("p", "paymentservice", "c", "hipstershop.PaymentService/Charge"),
            ("pc", "productcatalogservice", "r", "hipstershop.ProductCatalogService/Get"),
            ("pc2", "productcatalogservice", "c", "hipstershop.ProductCatalogService/Get"),
            ("x", "frontendservice", "f", "grpc.health.v1.Health/Check"),
            ("y", "frontendservice", "f", "acme.Unknown/Call")]
    cands = ["frontendservice", "checkoutservice", "currencyservice", "recommendationservice",
             "adservice", "emailservice", "paymentservice", "productcatalogservice", "redis"]
    return _case(tmp_path, rows, root, cands)


def test_client_spans_add_the_edges_of_span_less_services_and_unknown_names_are_not_guessed(tmp_path):
    case = _boutique(tmp_path, "checkoutservice")
    edges, rep = case.call_edges
    assert ("frontendservice", "adservice") in edges
    assert ("frontendservice", "adservice") not in case.edges          # edges stays server-only
    assert rep["unmapped_grpc_names"] == {"acme.Unknown": 1}
    assert not any("health" in b.lower() or b == "acme" for _, b in edges)


def test_a_dev_like_root_cause_has_no_distant_service(tmp_path):
    cls = distance_classes(_boutique(tmp_path, "checkoutservice"))
    assert cls["frontendservice"][0] == "ancestor"
    assert cls["adservice"][0] == "neighbour"                    # sibling via the client-span edge
    assert cls["redis"] == ("unknown", None)                      # not in the graph: never eligible
    assert not any(c == "distant" for c, _ in cls.values())


def test_an_email_like_root_cause_has_distant_services_at_three_hops(tmp_path):
    cls = distance_classes(_boutique(tmp_path, "emailservice"))
    assert cls["recommendationservice"] == ("distant", 3)
    assert cls["adservice"] == ("distant", 3)
    assert cls["checkoutservice"][0] == cls["frontendservice"][0] == "ancestor"
    assert cls["paymentservice"][0] == "neighbour"
    assert cls["productcatalogservice"][0] == "neighbour"        # sibling under checkout


def test_a_grandchild_is_near_other_not_neighbour_and_not_distant(tmp_path):
    cls = distance_classes(_boutique(tmp_path, "frontendservice"))
    assert cls["emailservice"] == ("near_other", 2)


def test_check_c_excludes_every_ancestor_not_only_direct_callers(tmp_path, monkeypatch):
    """Decisions log 28 Nov: an ancestor two calls up inherits the root cause's symptoms as a
    direct caller does. frontend -> checkout -> email: for root email, checkout is a direct caller
    and frontend an ancestor; both fail (c). payment (a sibling) passes it."""
    case = _boutique(tmp_path, "emailservice")
    assert case.ancestors_of("emailservice") == {"checkoutservice", "frontendservice"}
    import provrca_bench.framebench.target as tg
    monkeypatch.setattr(tg, "baro_scores", lambda case: {})      # (a) and (d) need metrics/logs
    case.__dict__["log_containers"] = set()
    c = {x.service: x for x in check_candidates(case)}
    assert not c["checkoutservice"].c_not_upstream_caller
    assert not c["frontendservice"].c_not_upstream_caller
    assert c["paymentservice"].c_not_upstream_caller


def test_the_subset_is_seeded_rounded_up_and_empty_without_eligible_cases():
    ids = [f"c{i}" for i in range(15)]
    s = distant_subset(ids, 7)
    assert len(s) == 2 and s == distant_subset(list(reversed(ids)), 7) and set(s) <= set(ids)
    assert distant_subset([], 7) == []
    assert len(distant_subset(ids[:3], 7)) == 1


# --------------------------------------------------------------------------------------------
# Real data
# --------------------------------------------------------------------------------------------

real = pytest.mark.skipif(not (SEALED / "targets.csv").exists(), reason="no development set")


@pytest.fixture(scope="module")
def dev_cases():
    dev = {json.loads(l)["source"].split("/")[0] for l in ANSWERS.read_text().splitlines()}
    cases = load_dev_cases(dataset_root=DATASET, tasks_root=TASKS, answers_jsonl=ANSWERS,
                           dev_templates=dev)
    return cases if os.environ.get("PROVRCA_FULL") else cases[::4]


@real
def test_no_development_case_has_a_distant_target(dev_cases):
    for case in dev_cases:
        assert not any(c == "distant" for c, _ in distance_classes(case).values()), case.case_id


@real
def test_the_main_target_rule_is_unchanged(dev_cases):
    """Review change 3: call_edges is new and edges is untouched, so every candidate's checks and
    shape equal the sealed targets.csv the development set was built from."""
    sealed = {(r["case_id"], r["service"]): r for r in csv.DictReader((SEALED / "targets.csv").open())}
    for case in dev_cases:
        for c in check_candidates(case):
            r = sealed[(case.case_id, c.service)]
            assert (c.shape, str(c.eligible), str(c.c_not_upstream_caller)) == \
                (r["shape"], r["eligible"], r["c_not_upstream_caller"]), (case.case_id, c.service)
