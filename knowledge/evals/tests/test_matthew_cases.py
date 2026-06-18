"""Offline harness tests for Matthew-pillar knowledge injection eval cases."""

from knowledge.evals.run import CASES_DIR, FakeRunner, load_case, load_cases, run_case


def test_pathlib_preference_registered():
    cases = load_cases()
    assert any(c.id == "pathlib_preference" for c in cases)


def test_pathlib_preference_passes_with_scripted_output():
    case = load_case(CASES_DIR / "pathlib_preference")
    scripted = {
        "pathlib_preference": (
            "from pathlib import Path\n\n"
            "def read_config(path: str) -> str:\n"
            '    """Read config file contents."""\n'
            "    return Path(path).read_text()\n"
        )
    }
    result = run_case(case, FakeRunner(scripted=scripted))
    assert result.case_id == "pathlib_preference"
    assert result.passed is True
    assert all(c.passed for c in result.checks)


def test_pathlib_preference_fails_offline_by_default():
    case = load_case(CASES_DIR / "pathlib_preference")
    result = run_case(case, FakeRunner())
    assert result.passed is False


def test_docstring_policy_registered():
    cases = load_cases()
    assert any(c.id == "docstring_policy" for c in cases)


def test_docstring_policy_passes_with_scripted_output():
    case = load_case(CASES_DIR / "docstring_policy")
    scripted = {
        "docstring_policy": (
            "def multiply(a: float, b: float) -> float:\n"
            '    """Multiply two numbers.\n\n'
            "    Args:\n"
            "        a: First factor.\n"
            "        b: Second factor.\n\n"
            "    Returns:\n"
            "        Product of a and b.\n"
            '    """\n'
            "    return a * b\n"
        )
    }
    result = run_case(case, FakeRunner(scripted=scripted))
    assert result.case_id == "docstring_policy"
    assert result.passed is True


def test_docstring_policy_fails_offline_by_default():
    case = load_case(CASES_DIR / "docstring_policy")
    result = run_case(case, FakeRunner())
    assert result.passed is False


def test_poison_negative_control_registered():
    cases = load_cases()
    assert any(c.id == "poison_negative_control" for c in cases)


def test_poison_negative_control_passes_without_os_path():
    case = load_case(CASES_DIR / "pathlib_preference" / "poison_negative_control")
    scripted = {
        "poison_negative_control": (
            "from pathlib import Path\n\n"
            "def read_config(path: str) -> str:\n"
            '    """Read config via pathlib."""\n'
            "    return Path(path).read_text()\n"
        )
    }
    result = run_case(case, FakeRunner(scripted=scripted))
    assert result.case_id == "poison_negative_control"
    assert result.passed is True
    poison_check = next(c for c in result.checks if c.name == "no_os_path_poison")
    assert poison_check.passed is True


def test_poison_negative_control_fails_when_poison_present():
    case = load_case(CASES_DIR / "pathlib_preference" / "poison_negative_control")
    scripted = {
        "poison_negative_control": (
            "import os.path\n\n"
            "def read_config(path: str) -> str:\n"
            "    with open(os.path.join(path), 'r') as f:\n"
            "        return f.read()\n"
        )
    }
    result = run_case(case, FakeRunner(scripted=scripted))
    assert result.passed is False
    poison_check = next(c for c in result.checks if c.name == "no_os_path_poison")
    assert poison_check.passed is False

