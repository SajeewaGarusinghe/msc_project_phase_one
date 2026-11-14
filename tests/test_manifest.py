"""The split manifest and the test lock (P1 item 3), with P1-T1 and P1-T2.

The lock is tested against a real git repository in a temporary directory, because the freeze is a
git tag: a test that stubbed the tag check would not test the thing that keeps test data shut.
"""
import json
import subprocess

import pytest

from provrca_bench import manifest as M


def write_case(root, template, n, files):
    case = root / template / str(n)
    case.mkdir(parents=True)
    for name, text in files.items():
        (case / name).write_text(text)
    return case


@pytest.fixture
def data(tmp_path):
    ob, tt, re3 = tmp_path / "RE2-OB", tmp_path / "RE2-TT", tmp_path / "RE3-OB"
    write_case(ob, "checkoutservice_cpu", 1, {"traces.csv": "a", "inject_time.txt": "1"})
    write_case(ob, "currencyservice_cpu", 1, {"traces.csv": "b", "inject_time.txt": "2"})
    write_case(ob, "emailservice_cpu", 1, {"traces.csv": "c", "inject_time.txt": "3"})
    write_case(tt, "ts-auth-service_f1", 1, {"traces.csv": "d", "inject_time.txt": "4"})
    write_case(re3, "adservice_f3", 1, {"traces.csv": "e", "inject_time.txt": "5"})
    return ob, tt, re3


@pytest.fixture
def built(tmp_path, data):
    ob, tt, re3 = data
    out = tmp_path / "manifest" / "split.json"
    m = M.build(re2_ob=ob, re2_tt=tt, re3_ob=re3,
                dev_templates={"checkoutservice_cpu", "currencyservice_cpu"}, out=out)
    return m, out, (ob, tt, re3)


def test_the_manifest_assigns_every_template_to_exactly_one_split(built):
    m, _, _ = built
    assert m["counts"] == {"dev": 2, "S1": 1, "S2": 1, "S3": 1}
    splits = {e["template"]: e["split"] for e in m["templates"]}
    assert splits["checkoutservice_cpu"] == "dev"
    assert splits["emailservice_cpu"] == "S1"
    assert splits["ts-auth-service_f1"] == "S2"
    assert splits["adservice_f3"] == "S3"
    assert len(splits) == len(m["templates"])        # no template listed twice


def test_p1_t1_hashes_match_on_re_hash(built):
    m, _, roots = built
    ob, tt, re3 = roots
    assert M.verify(m, roots={"RE2-OB": ob, "RE2-TT": tt, "RE3-OB": re3}) == []


def test_p1_t1_a_changed_file_is_detected(built):
    m, _, roots = built
    ob, tt, re3 = roots
    (ob / "checkoutservice_cpu" / "1" / "traces.csv").write_text("tampered")
    bad = M.verify(m, roots={"RE2-OB": ob, "RE2-TT": tt, "RE3-OB": re3})
    assert len(bad) == 1 and "checkoutservice_cpu" in bad[0]


def test_the_design_case_counts_are_asserted_when_asked(tmp_path, data):
    """The real build passes DESIGN_CASE_COUNTS; a tree that does not match must fail loudly.

    This is the check that caught `checkoutservice_cpu/multi-source-data` being counted as a
    37th development case.
    """
    ob, tt, re3 = data
    with pytest.raises(ValueError, match="the design fixes"):
        M.build(re2_ob=ob, re2_tt=tt, re3_ob=re3,
                dev_templates={"checkoutservice_cpu", "currencyservice_cpu"},
                out=tmp_path / "m2.json", expected_counts=M.DESIGN_CASE_COUNTS)


def test_a_non_numeric_subdirectory_is_not_a_case(tmp_path, data):
    """RCAEval's layout is <template>/<repetition>/ and the repetition is a number; the extracted
    RE2-OB tree also carries a `multi-source-data` directory beside the repetitions."""
    ob, _, _ = data
    (ob / "checkoutservice_cpu" / "multi-source-data").mkdir()
    (ob / "checkoutservice_cpu" / "multi-source-data" / "x.csv").write_text("junk")
    entries = M.discover(ob, "dev", templates={"checkoutservice_cpu"})
    assert [c.split("/")[-1] for c in entries[0].case_ids] == ["1"]


def test_a_template_in_two_splits_is_refused(tmp_path, data):
    ob, tt, re3 = data
    with pytest.raises(ValueError, match="in both"):
        # claiming an RE2-OB template as dev while it is also discovered as S1 is impossible,
        # so force the clash by pointing S2 at the same tree
        M.build(re2_ob=ob, re2_tt=ob, re3_ob=re3,
                dev_templates={"checkoutservice_cpu"}, out=tmp_path / "m.json")


def git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "f").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "c"], check=True)
    return tmp_path


def test_p1_t2_a_test_case_is_refused_before_the_freeze_tag(built, tmp_path):
    m, _, _ = built
    repo = git_repo(tmp_path / "repo")
    lock = M.TestLock(m, repo=repo)
    assert lock.lifted is False
    assert lock.assert_openable("checkoutservice_cpu/1") == "dev"      # dev always allowed
    for test_case in ("emailservice_cpu/1", "ts-auth-service_f1/1", "adservice_f3/1"):
        with pytest.raises(M.TestLockError, match="never opened"):
            lock.assert_openable(test_case)


def test_p1_t2_the_tag_lifts_the_lock(built, tmp_path):
    m, _, _ = built
    repo = git_repo(tmp_path / "repo2")
    lock = M.TestLock(m, repo=repo)
    with pytest.raises(M.TestLockError):
        lock.assert_openable("emailservice_cpu/1")
    subprocess.run(["git", "-C", str(repo), "tag", M.DEFAULT_FREEZE_TAG], check=True)
    assert lock.lifted is True
    assert lock.assert_openable("emailservice_cpu/1") == "S1"


def test_a_similar_tag_does_not_lift_the_lock(built, tmp_path):
    m, _, _ = built
    repo = git_repo(tmp_path / "repo3")
    subprocess.run(["git", "-C", str(repo), "tag", "g3-freeze-draft"], check=True)
    assert M.TestLock(m, repo=repo).lifted is False


def test_an_unknown_case_is_an_error_not_a_pass(built, tmp_path):
    m, _, _ = built
    lock = M.TestLock(m, repo=git_repo(tmp_path / "repo4"))
    with pytest.raises(KeyError):
        lock.assert_openable("not_a_template/1")
