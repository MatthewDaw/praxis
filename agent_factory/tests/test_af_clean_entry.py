"""R50 — af-clean exposes two entry points over one engine.

Covers the ticket's acceptance condition: ``/af-clean`` with no argument scopes to the repo root
and ``/af-clean <path>`` scopes to that subtree; E1 invokes the validation step and returns
findings without emitting a pass/fail verdict; E2 receives the ticket diff only, applies no
advise-tier finding, and does not invoke the validation step; and a dry-run invocation applies
nothing while producing the same findings.
"""

from __future__ import annotations

from agent_factory.af_clean.entry import E1Result, E2Result, resolve_scope, run_e1, run_e2
from agent_factory.af_clean.findings import Finding, Location
from agent_factory.af_clean_validate import PASSED, ValidateRemediateReport


def _finding(rule="r", tier="enforce", file="a.py", line=1, pole="bloat"):
    return Finding(rule=rule, tier=tier, location=Location(file=file, line=line), pole=pole)


def _always_pass_runner(argv, cwd):
    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""
    return _Proc()


# --------------------------------------------------------------------------- scope resolution

def test_no_argument_scopes_to_repo_root(tmp_path):
    assert resolve_scope(tmp_path) == tmp_path
    assert resolve_scope(tmp_path, None) == tmp_path
    assert resolve_scope(tmp_path, "") == tmp_path


def test_path_argument_scopes_to_subtree(tmp_path):
    assert resolve_scope(tmp_path, "sub/dir") == tmp_path / "sub" / "dir"


def test_absolute_path_argument_used_as_is(tmp_path):
    other = tmp_path / "elsewhere"
    assert resolve_scope(tmp_path, str(other)) == other


# --------------------------------------------------------------------------- E1

def test_e1_invokes_validation_step(tmp_path):
    calls = []

    def produce(scope):
        calls.append(scope)
        return [_finding()]

    result = run_e1(
        tmp_path,
        produce_findings=produce,
        runner=_always_pass_runner,
        validate_kwargs={"commands": {"test": "true", "typecheck": "true", "lint": "true"}},
    )
    assert isinstance(result, E1Result)
    assert calls == [tmp_path]
    assert isinstance(result.validation, ValidateRemediateReport)
    assert result.validation.overall_status == PASSED


def test_e1_returns_findings_without_a_pass_fail_verdict(tmp_path):
    result = run_e1(
        tmp_path,
        produce_findings=lambda scope: [_finding()],
        runner=_always_pass_runner,
        validate_kwargs={"commands": {}},
    )
    assert len(result.findings) == 1
    # The result itself carries no top-level verdict/passed field -- only findings + the
    # validation step's own (informational) report.
    field_names = {f.name for f in result.__dataclass_fields__.values()}
    assert "verdict" not in field_names
    assert "passed" not in field_names


def test_e1_scopes_to_caller_named_subtree(tmp_path):
    calls = []
    run_e1(
        tmp_path,
        "sub",
        produce_findings=lambda scope: calls.append(scope) or [],
        runner=_always_pass_runner,
        validate_kwargs={"commands": {}},
    )
    assert calls == [tmp_path / "sub"]


def test_e1_dry_run_applies_nothing_but_same_findings(tmp_path):
    applied_calls = []

    def apply_findings(findings):
        applied_calls.append(findings)

    live = run_e1(
        tmp_path,
        produce_findings=lambda scope: [_finding()],
        apply_findings=apply_findings,
        runner=_always_pass_runner,
        validate_kwargs={"commands": {}},
    )
    dry = run_e1(
        tmp_path,
        produce_findings=lambda scope: [_finding()],
        apply_findings=apply_findings,
        dry_run=True,
        runner=_always_pass_runner,
        validate_kwargs={"commands": {}},
    )
    assert dry.findings == live.findings
    assert dry.applied is False
    assert live.applied is True
    assert len(applied_calls) == 1  # only the live run ever called apply_findings


# --------------------------------------------------------------------------- E2

def test_e2_receives_only_the_diff(tmp_path):
    seen = []

    def produce(diff):
        seen.append(diff)
        return [_finding()]

    result = run_e2("--- a/x.py\n+++ b/x.py\n", produce_findings=produce)
    assert seen == ["--- a/x.py\n+++ b/x.py\n"]
    assert isinstance(result, E2Result)
    assert len(result.findings) == 1


def test_e2_applies_no_advise_tier_finding(tmp_path):
    advise = _finding(rule="advise-rule", tier="advise")
    result = run_e2("diff", produce_findings=lambda diff: [advise])
    assert advise not in result.applied
    assert result.applied == ()
    assert advise in result.findings


def test_e2_applies_no_enforce_tier_finding_either_report_only_per_D8(tmp_path):
    enforce = _finding(rule="enforce-rule", tier="enforce")
    result = run_e2("diff", produce_findings=lambda diff: [enforce])
    assert result.applied == ()
    assert enforce in result.reported


def test_e2_does_not_invoke_the_validation_step(tmp_path):
    # run_e2's signature carries no repo path / runner / validate_kwargs at all -- there is
    # nothing to invoke the validation step WITH, which is the point.
    import inspect
    sig = inspect.signature(run_e2)
    assert set(sig.parameters) == {"ticket_diff", "produce_findings"}
    # And the result object never carries a validation report field.
    field_names = {f.name for f in E2Result.__dataclass_fields__.values()}
    assert "validation" not in field_names
