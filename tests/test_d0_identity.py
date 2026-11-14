"""P1-T4: D0's output must be byte-identical to the raw CaseToolkit output.

The test uses 20 pseudo-random tool calls over the output shapes RCAEval's toolkit actually
returns -- a DataFrame-like table, a dict, a list of dicts, a string and None -- and compares
*bytes*, not equality. A layer that reserialised or normalised its input would pass an `==`
check and fail this one, which is the point of the test.
"""
import pickle
import random

from provrca_bench.defence import D0

TOOLS = ["describe_case", "read_metrics", "read_logs", "read_traces", "call_graph",
         "rank_anomalies", "log_template"]


def outputs(rng):
    """Output shapes in the spirit of CaseToolkit's returns."""
    return [
        {"services": ["frontend", "cartservice"], "n_rows": rng.randint(1, 10_000)},
        [{"service": "frontend", "z": rng.random()} for _ in range(rng.randint(1, 5))],
        "time,frontend_latency-50\n1705353846,12.5\n",
        None,
        rng.random(),
        [],
        {"nested": {"a": [1, 2, {"b": None}]}},
    ]


def test_d0_returns_the_same_object_and_the_same_bytes():
    rng = random.Random(20261114)
    layer = D0()
    for i in range(20):
        tool = rng.choice(TOOLS)
        args = {"case_id": f"re2-ob-{i:04d}", "k": rng.randint(1, 5)}
        raw = rng.choice(outputs(rng))
        out, verdicts = layer.filter(tool, args, raw, sender="frontend")
        assert out is raw, f"{tool}: D0 must return the same object, not a copy"
        assert pickle.dumps(out) == pickle.dumps(raw), f"{tool}: bytes differ"
        assert verdicts == [], "D0 forms no verdicts"


def test_d0_does_not_depend_on_the_sender():
    layer = D0()
    raw = {"a": 1}
    for sender in (None, "frontend", "attacker-pod"):
        out, verdicts = layer.filter("read_traces", {}, raw, sender=sender)
        assert out is raw and verdicts == []


def test_d0_declares_its_arm():
    assert D0().arm == "D0"
