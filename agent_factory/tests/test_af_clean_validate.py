"""R31 — af-clean's validation-and-remediation step is decomposable and callable standalone.

Covers the ticket's acceptance condition: the step runs its eight named phases IN ORDER (full test
suite, typecheck, lint, AST reference sweep, reachability re-check, the building-validation lane
when present, a bounded remediation loop, an advisory coverage report); is callable with no
af-clean run in progress (a plain function over explicit inputs, no shared/global state); discovers
and calls the repo's REAL test/lint/typecheck commands rather than assuming just a recipe runner;
ESCALATES on remediation-cap exhaustion rather than passing; and warns when the target repo
enforces a CI coverage floor that deletions would trip.
"""

from __future__ import annotations

from agent_factory.af_clean_validate import (
    ESCALATED,
    FAILED,
    PASSED,
    PHASES,
    SKIPPED,
    detect_coverage_floor,
    discover_commands,
    run_validation_and_remediation,
)


class _Proc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _always_pass_runner(argv, cwd):
    return _Proc(0)


def _always_fail_runner(argv, cwd):
    return _Proc(1, stderr="boom")


# --------------------------------------------------------------------------- phase ordering

def test_eight_named_phases_run_in_order(tmp_path):
    report = run_validation_and_remediation(tmp_path, runner=_always_pass_runner, commands={})
    assert [p.name for p in report.phases] == list(PHASES)
    assert len(PHASES) == 8


def test_overall_pass_when_everything_green(tmp_path):
    report = run_validation_and_remediation(
        tmp_path,
        runner=_always_pass_runner,
        commands={"test": "true", "typecheck": "true", "lint": "true"},
    )
    assert report.overall_status == PASSED
    assert report.passed is True


# --------------------------------------------------------------------------- standalone invocation

def test_callable_with_no_af_clean_run_in_progress(tmp_path):
    """No global/module state: two independent calls never interfere with each other."""
    r1 = run_validation_and_remediation(tmp_path, runner=_always_pass_runner, commands={"test": "true"})
    r2 = run_validation_and_remediation(tmp_path, runner=_always_fail_runner, commands={"test": "false"})
    assert r1.overall_status == PASSED
    assert r2.overall_status == ESCALATED
    # r1's success is untouched by r2 running after it.
    assert r1.overall_status == PASSED


# --------------------------------------------------------------------------- command discovery

def test_discovers_npm_script_over_justfile_recipe(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}')
    (tmp_path / "justfile").write_text("test:\n    echo not-this\n")
    commands = discover_commands(tmp_path)
    assert commands["test"] == "npm run test"


def test_discovers_native_python_config_over_justfile_recipe(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")
    (tmp_path / "justfile").write_text("lint:\n    echo not-this\n")
    commands = discover_commands(tmp_path)
    assert commands["lint"] == "ruff check ."


def test_falls_back_to_makefile_target_when_nothing_native(tmp_path):
    (tmp_path / "Makefile").write_text("test:\n\tpytest\n")
    commands = discover_commands(tmp_path)
    assert commands["test"] == "make test"


def test_falls_back_to_justfile_only_as_last_resort(tmp_path):
    (tmp_path / "justfile").write_text("typecheck:\n    echo checking\n")
    commands = discover_commands(tmp_path)
    assert commands["typecheck"] == "just typecheck"


def test_missing_capability_stays_none_not_fabricated(tmp_path):
    commands = discover_commands(tmp_path)
    assert commands == {"test": None, "typecheck": None, "lint": None}


# --------------------------------------------------------------------------- building-validation lane

def test_building_validation_lane_skipped_when_absent(tmp_path):
    report = run_validation_and_remediation(
        tmp_path, runner=_always_pass_runner, commands={}, building_validation_checks=None
    )
    lane = [p for p in report.phases if p.name == "building_validation_lane"][0]
    assert lane.status == SKIPPED
    assert report.building_validation_results == []


def test_building_validation_lane_runs_when_present(tmp_path):
    checks = [{"meta": {"check_id": "chk-1", "run": "true"}}]
    report = run_validation_and_remediation(
        tmp_path, runner=_always_pass_runner, commands={}, building_validation_checks=checks
    )
    lane = [p for p in report.phases if p.name == "building_validation_lane"][0]
    assert lane.status == PASSED
    assert report.building_validation_results[0].name == "chk-1"
    assert report.building_validation_results[0].status == PASSED


# --------------------------------------------------------------------------- remediation loop

def test_remediation_succeeds_within_cap(tmp_path):
    calls = {"n": 0}

    def runner(argv, cwd):
        calls["n"] += 1
        # Fail the very first invocation (the initial test-suite run), pass every retry after.
        return _Proc(1) if calls["n"] == 1 else _Proc(0)

    def remediate(name, phase_result):
        return True  # claims to have fixed it; the retry above then reports success

    report = run_validation_and_remediation(
        tmp_path,
        runner=runner,
        commands={"test": "true"},
        remediate=remediate,
        iteration_cap=3,
    )
    assert report.overall_status == PASSED
    assert report.remediation_attempts == 1


def test_escalates_on_cap_exhaustion_rather_than_passing(tmp_path):
    report = run_validation_and_remediation(
        tmp_path,
        runner=_always_fail_runner,
        commands={"test": "false"},
        remediate=lambda name, pr: False,  # can never actually fix it
        iteration_cap=2,
    )
    assert report.overall_status == ESCALATED
    assert report.passed is False
    assert report.remediation_attempts == 2
    remediation_phase = [p for p in report.phases if p.name == "remediation_loop"][0]
    assert remediation_phase.status == FAILED


def test_escalates_when_no_remediation_callback_and_a_phase_fails(tmp_path):
    report = run_validation_and_remediation(
        tmp_path, runner=_always_fail_runner, commands={"test": "false"}
    )
    assert report.overall_status == ESCALATED


# --------------------------------------------------------------------------- coverage floor warning

def test_warns_on_pyproject_coverage_floor(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.coverage.report]\nfail_under = 90\n")
    warning = detect_coverage_floor(tmp_path)
    assert warning is not None
    assert "90" in warning


def test_no_warning_when_no_coverage_floor_configured(tmp_path):
    assert detect_coverage_floor(tmp_path) is None


def test_coverage_floor_warning_surfaces_in_report_without_gating(tmp_path):
    (tmp_path / ".coveragerc").write_text("[report]\nfail_under = 80\n")
    report = run_validation_and_remediation(tmp_path, runner=_always_pass_runner, commands={})
    assert any("80" in w for w in report.warnings)
    assert report.overall_status == PASSED  # advisory only — never blocks
