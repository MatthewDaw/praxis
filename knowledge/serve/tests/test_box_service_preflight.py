"""R17: the box service pins the Claude Code CLI version it was validated
against and, on startup, probes every relied-upon capability — background
launch, the session listing's fields and state vocabulary, per-dispatch hook
injection, the terminal event, and resume — refusing to claim any job when
the pinned version does not match the installed one or when any probe fails,
and reporting the pinned version, the installed version, and the specific
failed probe. Asserted as pure decision logic (no live CLI), plus the default
capability wiring asserted against the named session-launcher seam with a
fake runner (no real background session ever started)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

import pytest

from knowledge.serve.box_service_preflight import (
    CAPABILITY_NAMES,
    PINNED_CLI_VERSION,
    PreflightError,
    PreflightResult,
    build_default_probes,
    require_claimable,
    run_preflight,
)
from knowledge.serve.session_launcher import SessionLauncher


def _passing_probes() -> dict[str, bool]:
    return {name: (lambda: True) for name in CAPABILITY_NAMES}


def test_matching_version_and_all_probes_passing_claims_normally():
    result = run_preflight(PINNED_CLI_VERSION, _passing_probes(), pinned_version=PINNED_CLI_VERSION)

    assert result.ok is True
    assert result.pinned_version == PINNED_CLI_VERSION
    assert result.installed_version == PINNED_CLI_VERSION
    assert result.failed_probe is None
    require_claimable(result)  # must not raise


def test_version_mismatch_refuses_and_reports_pinned_and_installed_version():
    result = run_preflight("1.0.0", _passing_probes(), pinned_version="2.0.0")

    assert result.ok is False
    assert result.pinned_version == "2.0.0"
    assert result.installed_version == "1.0.0"
    assert result.failed_probe is not None
    report = result.report()
    assert "2.0.0" in report
    assert "1.0.0" in report


def test_version_mismatch_never_runs_capability_probes():
    calls: list[str] = []

    def spy(name: str):
        def _probe() -> bool:
            calls.append(name)
            return True

        return _probe

    probes = {name: spy(name) for name in CAPABILITY_NAMES}
    run_preflight("1.0.0", probes, pinned_version="2.0.0")

    assert calls == []


def test_probe_failure_refuses_and_reports_the_specific_failed_probe():
    probes = _passing_probes()
    failing_name = CAPABILITY_NAMES[2]
    probes[failing_name] = lambda: False

    result = run_preflight(PINNED_CLI_VERSION, probes, pinned_version=PINNED_CLI_VERSION)

    assert result.ok is False
    assert result.failed_probe == failing_name
    assert failing_name in result.report()


def test_probe_that_raises_counts_as_a_failure_not_an_unhandled_error():
    probes = _passing_probes()
    failing_name = CAPABILITY_NAMES[0]

    def _boom() -> bool:
        raise RuntimeError("cli surface changed")

    probes[failing_name] = _boom

    result = run_preflight(PINNED_CLI_VERSION, probes, pinned_version=PINNED_CLI_VERSION)

    assert result.ok is False
    assert result.failed_probe == failing_name


def test_first_failing_probe_in_declared_order_wins_when_several_fail():
    probes = _passing_probes()
    probes[CAPABILITY_NAMES[1]] = lambda: False
    probes[CAPABILITY_NAMES[3]] = lambda: False

    result = run_preflight(PINNED_CLI_VERSION, probes, pinned_version=PINNED_CLI_VERSION)

    assert result.failed_probe == CAPABILITY_NAMES[1]


def test_missing_probe_wiring_is_a_startup_defect_not_a_silent_skip():
    incomplete = _passing_probes()
    del incomplete[CAPABILITY_NAMES[-1]]

    with pytest.raises(KeyError):
        run_preflight(PINNED_CLI_VERSION, incomplete, pinned_version=PINNED_CLI_VERSION)


def test_require_claimable_raises_preflighterror_on_refusal():
    result = PreflightResult(
        ok=False, pinned_version="2.0.0", installed_version="1.0.0", failed_probe="resume"
    )

    with pytest.raises(PreflightError) as excinfo:
        require_claimable(result)

    assert excinfo.value.result is result
    assert "resume" in str(excinfo.value)


@dataclass
class FakeRunner:
    """Scripted per-argv-prefix responses, recording every invocation — the
    same fake-runner seam ``test_session_launcher_seam.py`` uses, so the
    default probes are asserted with no real background session ever
    started."""

    responses: dict[tuple[str, ...], subprocess.CompletedProcess] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        key = tuple(args)
        if key in self.responses:
            return self.responses[key]
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")


def _healthy_runner() -> FakeRunner:
    sessions = json.dumps(
        [
            {
                "session_id": "sess-1",
                "cwd": "/repo",
                "kind": "bg",
                "started_at": "2026-07-25T00:00:00Z",
                "name": "job-1",
                "state": "running",
            }
        ]
    )
    return FakeRunner(
        responses={
            ("claude", "--help"): subprocess.CompletedProcess(
                args=[], returncode=0, stdout="usage: claude [--bg] [--hooks FILE]", stderr=""
            ),
            ("claude", "agents", "--help"): subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="agents subcommands: resume, terminate; terminal states: completed, failed",
                stderr="",
            ),
            ("claude", "agents", "--json"): subprocess.CompletedProcess(
                args=[], returncode=0, stdout=sessions, stderr=""
            ),
        }
    )


def test_default_probes_all_pass_against_a_healthy_cli():
    launcher = SessionLauncher(runner=_healthy_runner(), cli="claude")
    probes = build_default_probes(launcher)

    assert set(probes) == set(CAPABILITY_NAMES)
    result = run_preflight(PINNED_CLI_VERSION, probes, pinned_version=PINNED_CLI_VERSION)
    assert result.ok is True


def test_default_background_launch_probe_fails_when_bg_flag_missing():
    runner = _healthy_runner()
    runner.responses[("claude", "--help")] = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="usage: claude [--hooks FILE]", stderr=""
    )
    launcher = SessionLauncher(runner=runner, cli="claude")
    probes = build_default_probes(launcher)

    result = run_preflight(PINNED_CLI_VERSION, probes, pinned_version=PINNED_CLI_VERSION)

    assert result.ok is False
    assert result.failed_probe == "background_launch"


def test_default_resume_probe_fails_when_resume_subcommand_missing():
    runner = _healthy_runner()
    runner.responses[("claude", "agents", "--help")] = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="agents subcommands: terminate; terminal states: completed, failed",
        stderr="",
    )
    launcher = SessionLauncher(runner=runner, cli="claude")
    probes = build_default_probes(launcher)

    result = run_preflight(PINNED_CLI_VERSION, probes, pinned_version=PINNED_CLI_VERSION)

    assert result.ok is False
    assert result.failed_probe == "resume"


def test_default_session_listing_probe_fails_on_unrecognized_state():
    runner = _healthy_runner()
    runner.responses[("claude", "agents", "--json")] = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(
            [
                {
                    "session_id": "sess-1",
                    "cwd": "/repo",
                    "kind": "bg",
                    "started_at": "2026-07-25T00:00:00Z",
                    "name": "job-1",
                    "state": "some-unknown-state",
                }
            ]
        ),
        stderr="",
    )
    launcher = SessionLauncher(runner=runner, cli="claude")
    probes = build_default_probes(launcher)

    result = run_preflight(PINNED_CLI_VERSION, probes, pinned_version=PINNED_CLI_VERSION)

    assert result.ok is False
    assert result.failed_probe == "session_listing_schema"
