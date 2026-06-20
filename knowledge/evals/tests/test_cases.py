"""Offline harness tests for registered knowledge-injection eval cases."""

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
            "    return a * b\n\n"
            "def test_multiply():\n"
            "    assert multiply(2, 3) == 6\n"
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
    case = load_case(CASES_DIR / "poison_negative_control")
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
    case = load_case(CASES_DIR / "poison_negative_control")
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


def test_quirky_exhaustive_switch_registered():
    cases = load_cases()
    assert any(c.id == "quirky_exhaustive_switch" for c in cases)


def test_quirky_exhaustive_switch_passes_with_scripted_output():
    case = load_case(CASES_DIR / "quirky_exhaustive_switch")
    scripted = {
        "quirky_exhaustive_switch": (
            'function statusLabel(status: "open" | "closed" | "pending"): string {\n'
            "  switch (status) {\n"
            '    case "open": return "Open";\n'
            '    case "closed": return "Closed";\n'
            '    case "pending": return "Pending";\n'
            "    default: {\n"
            "      const _exhaustive: never = status;\n"
            "      return _exhaustive;\n"
            "    }\n"
            "  }\n"
            "}\n"
        )
    }
    result = run_case(case, FakeRunner(scripted=scripted))
    assert result.case_id == "quirky_exhaustive_switch"
    assert result.passed is True


def test_quirky_exhaustive_switch_fails_offline_by_default():
    case = load_case(CASES_DIR / "quirky_exhaustive_switch")
    result = run_case(case, FakeRunner())
    assert result.passed is False


def test_quirky_config_load_order_registered():
    cases = load_cases()
    assert any(c.id == "quirky_config_load_order" for c in cases)


def test_quirky_config_load_order_passes_with_scripted_output():
    case = load_case(CASES_DIR / "quirky_config_load_order")
    scripted = {
        "quirky_config_load_order": (
            "# Set experimental_options in parent shell before launching nu\n"
            "$env.experimental_options = 'feature_x'\n"
            "nu\n"
        )
    }
    result = run_case(case, FakeRunner(scripted=scripted))
    assert result.case_id == "quirky_config_load_order"
    assert result.passed is True


def test_quirky_config_load_order_fails_offline_by_default():
    case = load_case(CASES_DIR / "quirky_config_load_order")
    result = run_case(case, FakeRunner())
    assert result.passed is False


def test_poison_negative_control_good_registered():
    cases = load_cases()
    assert any(c.id == "poison_negative_control_good" for c in cases)


def test_poison_negative_control_good_passes_with_scripted_output():
    case = load_case(CASES_DIR / "poison_negative_control_good")
    scripted = {
        "poison_negative_control_good": (
            "def divide(a: float, b: float) -> float:\n"
            '    """Divide a by b."""\n'
            "    return a / b\n\n"
            "def test_divide():\n"
            "    assert divide(10, 2) == 5\n"
        )
    }
    result = run_case(case, FakeRunner(scripted=scripted))
    assert result.case_id == "poison_negative_control_good"
    assert result.passed is True


def test_poison_negative_control_good_fails_offline_by_default():
    case = load_case(CASES_DIR / "poison_negative_control_good")
    result = run_case(case, FakeRunner())
    assert result.passed is False


def test_poison_negative_control_bad_registered():
    cases = load_cases()
    assert any(c.id == "poison_negative_control_bad" for c in cases)


def test_poison_negative_control_bad_passes_with_scripted_output():
    case = load_case(CASES_DIR / "poison_negative_control_bad")
    scripted = {
        "poison_negative_control_bad": (
            "def divide(a: float, b: float) -> float:\n"
            '    """Divide a by b."""\n'
            "    return a / b\n\n"
            "def test_divide():\n"
            "    assert divide(10, 2) == 5\n"
        )
    }
    result = run_case(case, FakeRunner(scripted=scripted))
    assert result.case_id == "poison_negative_control_bad"
    assert result.passed is True


def test_poison_negative_control_bad_fails_offline_by_default():
    case = load_case(CASES_DIR / "poison_negative_control_bad")
    result = run_case(case, FakeRunner())
    assert result.passed is False


def test_promote_then_rerun_registered():
    cases = load_cases()
    assert any(c.id == "promote_then_rerun" for c in cases)


def test_promote_then_rerun_passes_with_scripted_output():
    case = load_case(CASES_DIR / "promote_then_rerun")
    scripted = {
        "promote_then_rerun": (
            "# Set experimental_options in parent shell before launching nu\n"
            "$env.experimental_options = 'feature_x'\n"
            "nu\n"
        )
    }
    result = run_case(case, FakeRunner(scripted=scripted))
    assert result.case_id == "promote_then_rerun"
    assert result.passed is True


def test_promote_then_rerun_fails_offline_by_default():
    case = load_case(CASES_DIR / "promote_then_rerun")
    result = run_case(case, FakeRunner())
    assert result.passed is False


def test_decayed_lesson_ignored_registered():
    cases = load_cases()
    assert any(c.id == "decayed_lesson_ignored" for c in cases)


def test_decayed_lesson_ignored_passes_with_scripted_output():
    case = load_case(CASES_DIR / "decayed_lesson_ignored")
    scripted = {
        "decayed_lesson_ignored": (
            "from pathlib import Path\n\n"
            "def read_config(path: str) -> str:\n"
            '    """Read config file contents."""\n'
            "    return Path(path).read_text()\n"
        )
    }
    result = run_case(case, FakeRunner(scripted=scripted))
    assert result.case_id == "decayed_lesson_ignored"
    assert result.passed is True


def test_decayed_lesson_ignored_fails_offline_by_default():
    case = load_case(CASES_DIR / "decayed_lesson_ignored")
    result = run_case(case, FakeRunner())
    assert result.passed is False


def test_cross_session_rediscovery_registered():
    cases = load_cases()
    assert any(c.id == "cross_session_rediscovery" for c in cases)


def test_cross_session_rediscovery_passes_with_scripted_output():
    case = load_case(CASES_DIR / "cross_session_rediscovery")
    scripted = {
        "cross_session_rediscovery": (
            'function statusLabel(status: "open" | "closed" | "pending"): string {\n'
            "  switch (status) {\n"
            '    case "open": return "Open";\n'
            '    case "closed": return "Closed";\n'
            '    case "pending": return "Pending";\n'
            "    default: {\n"
            "      const _exhaustive: never = status;\n"
            "      return _exhaustive;\n"
            "    }\n"
            "  }\n"
            "}\n"
        )
    }
    result = run_case(case, FakeRunner(scripted=scripted))
    assert result.case_id == "cross_session_rediscovery"
    assert result.passed is True


def test_cross_session_rediscovery_fails_offline_by_default():
    case = load_case(CASES_DIR / "cross_session_rediscovery")
    result = run_case(case, FakeRunner())
    assert result.passed is False

