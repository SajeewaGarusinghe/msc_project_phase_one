"""A4: the information boundary, enforced by layout and a test rather than by discipline.

A4-T1 builds the real layout -- a sealed store owned by a separate Unix user at mode 0700, a
``cases/`` tree an arm can pass through but not list, keyed variant names -- and launches a probe
*as the arm user* through ``launch_arm``, the one function the runner may start an arm with. The
build fails unless every forbidden access is denied **and** the arm can still read its own
variant: without that control the test would pass on a layout nobody could use.

A4-T1 needs sudo and the two users from ``scripts/a4_users.sh``. Where they are missing it fails
under CI (or with PROVRCA_REQUIRE_A4=1) and is skipped elsewhere with the reason, so a workstation
without them can still run the unit tests but CI cannot go green without the boundary.

A4-T2 scans every prompt, tool output and trajectory of the G0 pilot (144 runs on development
cases) for the target beside a revealing word or a family code. The pilot's 14 GB of case data
cannot live in CI, so the scan is run with ``python -m provrca_bench.boundary`` and its summary is
committed in ``evidence/``; CI checks the scanner on planted leaks and checks the committed record.
"""
import json
import os

from pathlib import Path

import pytest
from pydantic import ValidationError

from provrca_bench import boundary as b
from provrca_bench.contracts import LabelRecord, TraceSpanRow

CASE_ROW = dict(time=1, traceID="t", spanID="s", serviceName="frontend",
                startTimeMillis=1000, startTime=1_000_000, duration=10, sender="frontend")
EVIDENCE = Path(__file__).resolve().parents[1] / "evidence" / "a4_t2_pilot.json"


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


# --- A4 item 3: variant names -------------------------------------------------------------

def test_a_variant_name_is_keyed_stable_and_reveals_nothing():
    key = b"k" * 32
    name = b.variant_dirname("F2-T2-high-7", key)
    assert name == b.variant_dirname("F2-T2-high-7", key)
    assert "F2" not in name and "high" not in name and len(name) == 16


def test_a_variant_name_cannot_be_guessed_without_the_key():
    """A plain hash of the variant string is what an arm could compute; the keyed name must
    differ from it, and from the name under any other key."""
    import hashlib
    plain = hashlib.sha256(b"F2-T2-high-7").hexdigest()[:16]
    assert b.variant_dirname("F2-T2-high-7", b"k" * 32) != plain
    assert b.variant_dirname("F2-T2-high-7", b"k" * 32) != b.variant_dirname("F2-T2-high-7",
                                                                             b"j" * 32)


# --- A4-T1: the layout, from inside an arm ------------------------------------------------

def _a4_t1_ready() -> str | None:
    if not b.sudo_available():
        return "passwordless sudo is not available"
    if not b.users_present(b.SEALER_USER, b.ARM_USER):
        return f"users {b.SEALER_USER} and {b.ARM_USER} do not exist; run scripts/a4_users.sh"
    return None


@pytest.fixture
def arm_layout():
    why = _a4_t1_ready()
    if why:
        if os.environ.get("CI") or os.environ.get("PROVRCA_REQUIRE_A4") == "1":
            pytest.fail(f"A4-T1 cannot run: {why}. The boundary is a build requirement.")
        pytest.skip(f"A4-T1 not runnable here: {why}")

    import tempfile
    # The base must be traversable by the arm user, so not pytest's 0700 tmp_path.
    base = Path(tempfile.mkdtemp(prefix="provrca-a4-", dir=os.environ.get("PROVRCA_A4_BASE",
                                                                           "/tmp")))
    os.chmod(base, 0o711)
    store = b.SealedStore(base / "sealed")
    key = store.create()
    store.write("labels.jsonl", b'{"case_id":"c1","target":"emailservice","family":"F2"}\n')
    variants = {v: {"traces.csv": b"time,traceID\n1,t\n"}
                for v in ("clean", "F2-T2-high-7", "F3-T2-low-7")}
    paths = b.lay_out_case(base / "cases", "c1", variants, key)
    yield base, store, paths
    store.remove()
    import shutil
    shutil.rmtree(base, ignore_errors=True)


def test_a4_t1_an_arm_cannot_open_a_sealed_file_or_list_sibling_variants(arm_layout):
    base, store, paths = arm_layout
    own = paths["F2-T2-high-7"]
    import hashlib
    guessed = own.parent / hashlib.sha256(b"F3-T2-low-7").hexdigest()[:16] / "traces.csv"
    # The system interpreter: the project's venv sits under the runner's home, which the arm
    # user cannot (and must not) reach.
    probe = b.launch_arm(["python3", "-I", "-c", b.PROBE, str(own / "traces.csv"),
                          str(store.path / "labels.jsonl"), str(own.parent),
                          str(base / "cases"), str(guessed)], cwd=own)
    assert probe.returncode == 0, probe.stderr
    seen = json.loads(probe.stdout)
    assert seen["read_own_variant"] == "allowed", "control: the arm must be able to work"
    assert seen["open_sealed_file"] == "denied"
    assert seen["list_sealed_store"] == "denied"
    assert seen["list_case_dir"] == "denied", "an arm must not list sibling variants"
    assert seen["list_cases_root"] == "denied", "an arm must not list other cases"
    assert seen["open_guessed_sibling"] in ("not_found", "denied")


def test_a4_t1_the_arm_runs_as_the_arm_user_with_no_inherited_environment(arm_layout):
    _, _, paths = arm_layout
    os.environ["PROVRCA_SECRET_PROBE"] = "leak"
    try:
        out = b.launch_arm(["/usr/bin/env"], cwd=paths["clean"])
    finally:
        del os.environ["PROVRCA_SECRET_PROBE"]
    assert "PROVRCA_SECRET_PROBE" not in out.stdout
    who = b.launch_arm(["/usr/bin/id", "-un"], cwd=paths["clean"])
    assert who.stdout.strip() == b.ARM_USER


# --- A4-T2: the scan ----------------------------------------------------------------------

@pytest.mark.parametrize("text,word", [
    ("the emailservice logs look fake to me", "fake"),
    ("records injected for EmailService at 12:00", "injected"),
    ("emailservice spans were forged", "forged"),
    ('{"service": "emailservice", "family": "F2"}', "F2"),
    ("variant FSc on emailservice", "FSc"),
    ("emailservice logs were injected", "injected"),     # injected *records*, not the fault
])
def test_a4_t2_scanner_catches_a_planted_leak(text, word):
    hits = b.scan_text(text, "emailservice")
    assert [h.word for h in hits] == [word]


@pytest.mark.parametrize("text", [
    "a fault was injected into the Online Boutique system; emailservice looks fine",
    "after the injection time emailservice CPU rose",           # 'injection' is legitimate
    # The agent echoing RCAEval's wording about the real fault: all 38 strict-scan pilot hits.
    "The fault was injected at 1705708871, causing emailservice latency",
    "A memory/resource fault was injected into emailservice at the inject time",
    "The injected fault targets emailservice",
    "emailservice trace 0xF1a2 and span F2a9",                  # hex is not a family code
    "fake news about cartservice",                              # a different service
    "emailservice" + " " * 200 + "fake",                        # outside the window
])
def test_a4_t2_scanner_ignores_legitimate_text(text):
    assert b.scan_text(text, "emailservice") == []


def test_a4_t2_the_pilot_scan_found_zero_hits_over_every_run():
    """The committed record of the pilot scan (rerun: python -m provrca_bench.boundary)."""
    if not EVIDENCE.exists():
        pytest.fail(f"{EVIDENCE} is missing: A4-T2 has not been run")
    s = json.loads(EVIDENCE.read_text())
    assert s["cases"] == 144, "every pilot run must be scanned"
    assert set(s["families"]) == {"clean", "F1", "F2", "F3"}
    assert s["tool_outputs_scanned"] > 0 and s["trajectory_lines_scanned"] > 0
    assert s["replay_mismatches"] == [], "the replay must reproduce what the agent saw"
    assert s["hits"] == []
    assert s["passed"] is True


def test_a4_t2_the_strict_scan_hit_only_the_agents_own_words_about_the_real_fault():
    """The unmasked scan is kept so the mask is auditable: every hit it found is in a trajectory
    (the agent's output), none in a prompt or tool output (what the agent could read), and every
    one is the word 'injected' -- never a family code or a forgery word."""
    strict = EVIDENCE.with_name("a4_t2_pilot_strict.json")
    s = json.loads(strict.read_text())
    assert s["hits"], "the strict record is the unmasked run"
    assert all(h["source"].startswith("trajectory:") for h in s["hits"])
    assert {h["word"].lower() for h in s["hits"]} == {"injected"}
    assert all(b.BENIGN_PHRASES.search(h["context"]) for h in s["hits"])
