"""A4: the information boundary, enforced by layout and a test rather than by discipline.

A4-T1 as the guide states it needs the real run layout: sealed labels in a directory owned by a
separate Unix user at mode 0700, and each arm running as a user that cannot read it. That cannot
be built before the runner exists (P1 item 3 onward), so what is checked here is the part that is
already decidable, and the rest is marked skipped with the reason rather than quietly passing:

  * no label type may be constructible from anything inside a case directory;
  * the contracts refuse a case row that carries a family, dose, seed or target column, which is
    how a label would most plausibly leak into the agent's view;
  * a variant directory name must not reveal family or dose (A4 item 3 hashes them).

A4-T2 greps prompts, tool outputs and trajectories for the target's name beside "fake",
"injected" or a family code. The pilot's trajectories live in `g0_pilot`, so that test runs there
or in CI once the runner writes into this repo; it is declared here so the gap is visible.
"""
import hashlib

import pytest
from pydantic import ValidationError

from provrca_bench.contracts import LabelRecord, TraceSpanRow

CASE_ROW = dict(time=1, traceID="t", spanID="s", serviceName="frontend",
                startTimeMillis=1000, startTime=1_000_000, duration=10, sender="frontend")


@pytest.mark.parametrize("leak", ["family", "dose", "seed", "target", "fabricated",
                                  "factually_false", "rule_applicable", "variant"])
def test_a_case_row_cannot_carry_a_label_field(leak):
    """The arms read case rows. A label field on one would cross the boundary silently."""
    with pytest.raises(ValidationError):
        TraceSpanRow(**CASE_ROW, **{leak: "F2"})


def test_a_label_record_needs_fields_no_case_file_contains():
    """A label cannot be reconstructed from a case directory: it needs the target, family, dose
    and seed, none of which appear in any case file."""
    with pytest.raises(ValidationError):
        LabelRecord(case_id="c1", record_id="s1", fabricated=True, factually_false=True,
                    rule_applicable=True)   # no variant, target, family, threat_model, dose, seed


def test_a_variant_directory_name_can_be_hashed_without_losing_the_mapping():
    """A4 item 3: variant directory names are hashed in the task view so family and dose cannot be
    read from the path. The hash must be stable and must not contain the family string."""
    variant = "F2-T2-high-7"
    hashed = hashlib.sha256(variant.encode()).hexdigest()[:16]
    assert "F2" not in hashed and "high" not in hashed
    assert hashed == hashlib.sha256(variant.encode()).hexdigest()[:16]


@pytest.mark.skip(reason="A4-T1 needs the runner and the separate Unix user from P1 item 3 "
                         "onward; it is not yet buildable and must not pass vacuously")
def test_a4_t1_an_arm_cannot_open_a_sealed_file_or_list_sibling_variants():
    raise AssertionError("unimplemented")


@pytest.mark.skip(reason="A4-T2 greps the dev pilot's prompts, tool outputs and trajectories, "
                         "which live in g0_pilot until the runner writes here")
def test_a4_t2_no_trajectory_names_the_target_beside_fake_injected_or_a_family_code():
    raise AssertionError("unimplemented")
