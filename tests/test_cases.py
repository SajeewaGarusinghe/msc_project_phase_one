"""The clean-case builder's hygiene guard (P1 item 4).

The guard is the part worth testing without the data: it must refuse before it copies anything,
and it must refuse on both of the two grounds that matter -- a sealed data set in the path, and a
template that is in the test split rather than the development one.
"""
import json

import pytest

from provrca_bench.cases import HygieneError, dev_templates, guard

DEV = {
    "seed": 20261109,
    "all_services": ["checkoutservice", "currencyservice", "emailservice"],
    "dev_services": ["checkoutservice", "currencyservice"],
    "n_templates": 4,
    "templates": [["checkoutservice", "cpu"], ["checkoutservice", "delay"],
                  ["currencyservice", "cpu"], ["currencyservice", "delay"]],
    "cases": ["re2-ob-0001", "re2-ob-0004"],
}


@pytest.fixture
def dev_file(tmp_path):
    path = tmp_path / "dev_cases.json"
    path.write_text(json.dumps(DEV))
    return path


def test_dev_templates_are_read_as_service_underscore_fault(dev_file):
    assert dev_templates(dev_file) == {"checkoutservice_cpu", "checkoutservice_delay",
                                       "currencyservice_cpu", "currencyservice_delay"}


def test_a_disagreeing_template_count_is_refused(tmp_path):
    bad = dict(DEV, n_templates=99)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(HygieneError, match="n_templates"):
        dev_templates(path)


def test_a_development_case_passes_the_guard(tmp_path, dev_file):
    case = tmp_path / "RE2-OB" / "checkoutservice_cpu" / "1"
    case.mkdir(parents=True)
    assert guard(case, dev_templates(dev_file)) == "checkoutservice_cpu"


def test_an_s1_template_is_refused(tmp_path, dev_file):
    case = tmp_path / "RE2-OB" / "emailservice_cpu" / "1"
    case.mkdir(parents=True)
    with pytest.raises(HygieneError, match="test stratum S1"):
        guard(case, dev_templates(dev_file))


@pytest.mark.parametrize("sealed", ["RE2-TT", "RE3-OB", "RE2-SS", "RE3-SS", "RE1-OB"])
def test_a_sealed_data_set_in_the_path_is_refused(tmp_path, dev_file, sealed):
    case = tmp_path / sealed / "checkoutservice_cpu" / "1"
    case.mkdir(parents=True)
    with pytest.raises(HygieneError, match="sealed or unused"):
        guard(case, dev_templates(dev_file))


def test_the_guard_refuses_before_anything_is_read(tmp_path, dev_file):
    """The directory need not even exist: the decision is made from the path."""
    with pytest.raises(HygieneError):
        guard(tmp_path / "RE2-OB" / "emailservice_disk" / "2", dev_templates(dev_file))
