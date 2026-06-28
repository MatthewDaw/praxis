"""Offline U5 tests: patch extraction (CRLF→LF), prompt assembly, arm config, loop.

Runs fully offline — a fake ``run_cli`` returns a committed ``--output-format json``
payload, a fake ``run_git`` returns a CRLF diff, and a stub ``grade`` drives the
rework loop. No real ``claude``, ``git``, Docker, or venv build is touched.

    uv run pytest knowledge/evals/swebench/tests/test_runner.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge.evals.swebench.instances import Instance, load_candidates
from knowledge.evals.swebench.runner import (
    _default_run_cli,
    build_mcp_config,
    build_prompt,
    extract_patch,
    run_arm,
)

FIX = Path(__file__).parent / "fixtures"


def _instance() -> Instance:
    records = json.loads((FIX / "rebench_sample.json").read_text(encoding="utf-8"))
    return {i.instance_id: i for i in load_candidates(records)}["sympy__sympy-fake-0001"]


class _FakeProc:
    def __init__(self, returncode, stdout, stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_run_cli_tolerates_error_max_turns(monkeypatch):
    # claude exits 1 on error_max_turns but the envelope (with usage) is still valid and
    # the tree may carry edits — the runner must return stdout, not raise (the live bug
    # the n=1 shakedown surfaced).
    import subprocess
    envelope = json.dumps({"subtype": "error_max_turns", "is_error": True,
                           "total_cost_usd": 0.46, "num_turns": 21, "usage": {"output_tokens": 7733}})
    monkeypatch.setattr("knowledge.evals.swebench.runner._claude_path", lambda: "claude")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(1, envelope))
    assert _default_run_cli(["-p", "x"], Path("."), {}, 10) == envelope


def test_run_cli_raises_on_real_crash(monkeypatch):
    # A non-zero exit with NO parseable result envelope is a genuine failure → raise.
    import subprocess
    monkeypatch.setattr("knowledge.evals.swebench.runner._claude_path", lambda: "claude")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(1, "segfault", "boom"))
    with pytest.raises(RuntimeError, match="claude exited 1"):
        _default_run_cli(["-p", "x"], Path("."), {}, 10)


def _sample_stdout() -> str:
    return (FIX / "claude_out.sample.json").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Patch extraction: the smoke-#3 CRLF→LF regression (the first failing test).
# ---------------------------------------------------------------------------
def test_extract_patch_normalizes_crlf_to_lf(tmp_path):
    """An agent edit on Windows yields a CRLF diff; extract_patch must emit pure LF.

    A raw ``git diff`` on a CRLF-edited file shows the whole file changed and won't
    apply in the Linux grader container. The fix is ``git add --renormalize`` then a
    cached diff — but the bytes that land in the patch must still be LF, never CRLF.
    """
    # A fake checkout dir; extract_patch shells git via the injected seam, so the
    # dir contents are irrelevant here — only run_git's output matters.
    crlf_diff = (
        "diff --git a/sympy/core/foo.py b/sympy/core/foo.py\r\n"
        "index 1111111..2222222 100644\r\n"
        "--- a/sympy/core/foo.py\r\n"
        "+++ b/sympy/core/foo.py\r\n"
        "@@ -1,2 +1,2 @@\r\n"
        "-old line\r\n"
        "+new line\r\n"
    )
    calls: list[list[str]] = []

    def fake_run_git(argv: list[str], cwd: Path) -> str:
        calls.append(argv)
        if argv[:2] == ["add", "--renormalize"]:
            return ""
        if argv[:2] == ["diff", "--cached"]:
            return crlf_diff
        return ""

    patch = extract_patch(tmp_path, run_git=fake_run_git)

    assert "\r" not in patch, "patch still contains CR — would not apply in Linux grader"
    assert "\r\n" not in patch
    assert patch.startswith("diff --git a/sympy/core/foo.py b/sympy/core/foo.py\n")
    assert "+new line\n" in patch
    # It went through the renormalize→cached-diff path, not a raw `git diff`.
    assert ["add", "--renormalize", "."] in calls or any(
        a[:2] == ["add", "--renormalize"] for a in calls
    )
    assert any(a[:2] == ["diff", "--cached"] for a in calls)


# ---------------------------------------------------------------------------
# Prompt assembly.
# ---------------------------------------------------------------------------
def test_build_prompt_includes_issue_excludes_gold_tests():
    inst = _instance()
    prompt = build_prompt(inst)
    assert inst.problem_statement in prompt
    # The agent writes its OWN repro; gold tests must never leak into the prompt.
    for gold in inst.fail_to_pass + inst.pass_to_pass:
        assert gold not in prompt
    assert "FAIL_TO_PASS" not in prompt
    assert inst.test_patch not in prompt  # gold test diff never shown


def test_build_prompt_rework_includes_issue_and_prior_repro():
    inst = _instance()
    repro = "def test_repro():\n    assert empty_matrix_mul() == []\n"
    prompt = build_prompt(inst, rework=True, prior_repro=repro)
    assert inst.problem_statement in prompt  # full issue text restated on rework
    assert repro in prompt  # the agent's own repro is fed back
    assert "still not resolved" in prompt.lower()
    # Still no gold leakage on the rework prompt.
    for gold in inst.fail_to_pass + inst.pass_to_pass:
        assert gold not in prompt
    assert "FAIL_TO_PASS" not in prompt


# ---------------------------------------------------------------------------
# Arm config: treatment pins the org via MCP; control has no Praxis MCP entry.
# ---------------------------------------------------------------------------
def test_treatment_mcp_config_pins_space():
    cfg = build_mcp_config("sympy__sympy-fake-0001", cache_path="/tmp/c.json")
    servers = cfg["mcpServers"]
    assert "praxis" in servers
    env = servers["praxis"]["env"]
    # The org rides in the cache file (PRAXIS_MCP_CACHE = the fixed eval org); the
    # per-instance space is pinned via the PRAXIS_SPACE env override (X-Praxis-Space).
    assert env["PRAXIS_MCP_CACHE"] == "/tmp/c.json"
    assert env["PRAXIS_SPACE"] == "sympy__sympy-fake-0001"


def test_control_has_no_praxis_mcp_entry():
    # Control passes no mcp config at all; run_arm must not wire one.
    inst = _instance()

    seen_args: list[list[str]] = []

    def fake_run_cli(args, cwd, env, timeout):
        seen_args.append(args)
        return _sample_stdout()

    def fake_run_git(argv, cwd):
        if argv[:2] == ["diff", "--cached"]:
            return "diff --git a/x b/x\n+1\n"
        return ""

    run_arm(
        inst,
        "control",
        grade=lambda patch: True,
        run_cli=fake_run_cli,
        run_git=fake_run_git,
        checkout=Path("/tmp/checkout"),
    )
    flat = " ".join(seen_args[0])
    assert "--mcp-config" not in flat
    assert "praxis" not in flat


# ---------------------------------------------------------------------------
# Cost / turns parsing.
# ---------------------------------------------------------------------------
def test_cost_and_turns_parsed_from_payload():
    inst = _instance()

    def fake_run_cli(args, cwd, env, timeout):
        return _sample_stdout()

    def fake_run_git(argv, cwd):
        if argv[:2] == ["diff", "--cached"]:
            return "diff --git a/x b/x\n+1\n"
        return ""

    res = run_arm(
        inst, "control", grade=lambda p: True,
        run_cli=fake_run_cli, run_git=fake_run_git, checkout=Path("/tmp/c"),
    )
    assert res.cost_usd == pytest.approx(0.1234567)
    assert res.turns == 7
    assert res.tokens == 512 + 2048
    assert res.resolved is True
    assert res.rework_rounds == 0


# ---------------------------------------------------------------------------
# Rework loop control flow (R13).
# ---------------------------------------------------------------------------
def test_passing_first_attempt_triggers_no_rework():
    inst = _instance()
    invocations = {"n": 0}

    def fake_run_cli(args, cwd, env, timeout):
        invocations["n"] += 1
        return _sample_stdout()

    def fake_run_git(argv, cwd):
        if argv[:2] == ["diff", "--cached"]:
            return "diff --git a/x b/x\n+1\n"
        return ""

    res = run_arm(
        inst, "control", grade=lambda p: True,
        run_cli=fake_run_cli, run_git=fake_run_git, checkout=Path("/tmp/c"), k_rework=1,
    )
    assert res.rework_rounds == 0
    assert invocations["n"] == 1  # one agent invocation, no rework


def test_failing_first_attempt_triggers_one_rework_at_k1():
    inst = _instance()
    invocations = {"n": 0}

    def fake_run_cli(args, cwd, env, timeout):
        invocations["n"] += 1
        return _sample_stdout()

    def fake_run_git(argv, cwd):
        if argv[:2] == ["diff", "--cached"]:
            return "diff --git a/x b/x\n+1\n"
        return ""

    # grade always fails → the loop should re-prompt exactly once (K=1) then stop.
    res = run_arm(
        inst, "control", grade=lambda p: False,
        run_cli=fake_run_cli, run_git=fake_run_git, checkout=Path("/tmp/c"), k_rework=1,
    )
    assert res.resolved is False
    assert res.rework_rounds == 1
    assert invocations["n"] == 2  # first attempt + one rework


def test_cost_accumulates_across_rework_rounds():
    inst = _instance()

    def fake_run_cli(args, cwd, env, timeout):
        return _sample_stdout()  # 0.1234567 each call

    def fake_run_git(argv, cwd):
        if argv[:2] == ["diff", "--cached"]:
            return "diff --git a/x b/x\n+1\n"
        return ""

    res = run_arm(
        inst, "control", grade=lambda p: False,
        run_cli=fake_run_cli, run_git=fake_run_git, checkout=Path("/tmp/c"), k_rework=1,
    )
    # first attempt + one rework → cost is cumulative (cost-to-correct within arm).
    assert res.cost_usd == pytest.approx(0.1234567 * 2)
    assert res.turns == 7 * 2


# ---------------------------------------------------------------------------
# ArmResult shape.
# ---------------------------------------------------------------------------
def test_treatment_records_retrieval_overhead_control_does_not():
    inst = _instance()

    def fake_run_cli(args, cwd, env, timeout):
        return _sample_stdout()

    def fake_run_git(argv, cwd):
        if argv[:2] == ["diff", "--cached"]:
            return "diff --git a/x b/x\n+1\n"
        return ""

    control = run_arm(
        inst, "control", grade=lambda p: True,
        run_cli=fake_run_cli, run_git=fake_run_git, checkout=Path("/tmp/c"),
    )
    treatment = run_arm(
        inst, "treatment", grade=lambda p: True,
        run_cli=fake_run_cli, run_git=fake_run_git, checkout=Path("/tmp/c"),
    )
    assert control.retrieval_overhead is None
    assert isinstance(treatment.retrieval_overhead, dict)
    assert control.arm == "control"
    assert treatment.arm == "treatment"
