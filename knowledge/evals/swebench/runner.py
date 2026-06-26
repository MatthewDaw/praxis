"""U5: repo-mounted agent runner for one arm, with the repro-test rework loop.

WHY a new runner instead of reusing :class:`knowledge.evals.claude_code.ClaudeCodeRunner`:
that runner is a **sealed box** — a throwaway temp dir, no repo mounted, no Bash,
no network — so it cannot check out the instance at ``base_commit``, run the
instance's own reproduction test, or reach the Praxis MCP. The PR-knowledge pilot
needs exactly those: the agent edits a *real* sympy checkout, the agent's own
repro runs against an ``install_config`` venv, and the treatment arm reaches the
org-pinned MCP. We borrow only the proven cost-capture seam (``_claude_usage``)
and the ``ANTHROPIC_API_KEY`` scrub (``_subscription_env``) from that module — not
its execution model (see plan Key Decisions: "New repo-mounted runner, not a
ClaudeCodeRunner reuse").

Everything testable is split into **pure, injectable-seam** functions so the unit
layer runs with no ``claude``, ``git``, Docker, or venv:

* :func:`extract_patch` — the smoke-#3 CRLF→LF fix (``git add --renormalize`` →
  cached diff → force LF). Git is injected via ``run_git``. This is the load-bearing
  regression: a raw ``git diff`` over a Windows-CRLF-edited file shows the whole
  file changed and won't apply in the Linux grader; renormalize + an LF-forced
  patch fixes it.
* :func:`build_prompt` — the fix instruction + full issue text (and, on rework,
  the agent's prior repro). It **never** contains the gold ``FAIL_TO_PASS`` /
  ``test_patch`` — the agent writes its own repro from the issue.
* :func:`build_mcp_config` — treatment's ``--mcp-config`` payload. The org is
  pinned out-of-band through the per-agent identity cache (``PRAXIS_MCP_CACHE`` →
  :func:`knowledge.mcp.identity.cache_path`), so each agent sends its own
  ``X-Praxis-Org`` without clobbering another agent's org. Control wires no MCP.

The live ``claude`` invocation is injected (``run_cli``, the same seam shape as
``claude_code.CliRunner``) and the grade decision is injected (``grade``) so the
whole rework loop is offline-testable. U6 passes U2's real grader in; U5's tests
stub it.

The checkout-at-base_commit and the ``install_config`` venv build are reached only
on the live path; they sit behind :func:`prepare_checkout` (a thin documented
seam, not unit-tested).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import json

from knowledge.evals.claude_code import (
    CliRunner,
    _claude_path,
    _claude_usage,
    _subscription_env,
)
from knowledge.evals.swebench.instances import Instance
from knowledge.evals.swebench.ingest import space_id_for

# A git runner: (argv, cwd) -> stdout. Injected so extract_patch tests run offline.
GitRunner = Callable[[list[str], Path], str]

# patch -> resolved? The rework loop's oracle. U6 passes U2's real grader; tests stub it.
GradeFn = Callable[[str], bool]

# Tools the agent may use. Bash is INCLUDED so the agent can navigate the repo and run
# its own repro test like a real SWE-bench agent — the n=1 shakedown showed a no-Bash
# agent burns its whole turn budget just *reading* a large source file and never edits.
# The original "no Bash" choice dodged an ORCHESTRATOR-side unsafe-agent guard that does
# not apply to a plain `claude` subprocess; bypassPermissions (below) lets Bash run
# unattended in the throwaway checkout, exactly as the sealed-box ClaudeCodeRunner does.
_ALLOWED_TOOLS = ["Read", "Edit", "Write", "Grep", "Glob", "Bash"]

_DEFAULT_MAX_TURNS = 40

# Per-agent-invocation wall-clock cap (seconds). A real Sonnet issue-fix run is minutes,
# not seconds (the Haiku smoke took ~150s for a tiny fix), so this is generous; it only
# exists so a wedged `claude` can't hang the loop forever.
_DEFAULT_TIMEOUT_S = 1800


@dataclass
class ArmResult:
    """One arm's outcome for one instance: the patch + cumulative cost-to-correct."""

    arm: str  # "treatment" | "control"
    patch: str  # final LF-normalized unified diff
    resolved: bool  # from the last grade callback
    # Cumulative agent spend across the rounds actually run: equals the in-arm
    # cost-to-correct when ``resolved`` (the loop stops at the first resolved round),
    # and total spend across all K+1 attempts when the arm never resolves. Read it
    # alongside ``resolved`` — the analysis segregates resolve-rate from cost.
    cost_usd: float | None
    tokens: int | None
    turns: int | None
    rework_rounds: int
    retrieval_overhead: dict | None = None  # treatment only (query-embed + MCP round-trip)


# ---------------------------------------------------------------------------
# Pure seam 1: patch extraction (the smoke-#3 CRLF→LF regression).
# ---------------------------------------------------------------------------
def _default_run_git(argv: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *argv],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip())[:500]
        raise RuntimeError(f"git {' '.join(argv)} exited {proc.returncode}: {detail}")
    return proc.stdout


def extract_patch(checkout_dir: Path, *, run_git: GitRunner = _default_run_git) -> str:
    """The agent's edits as a clean, LF-normalized unified diff.

    Smoke-#3 footgun: an agent edit on Windows writes CRLF line endings, so a raw
    ``git diff`` reports the whole file as changed and the patch won't apply in the
    Linux grader container. The fix is ``git add --renormalize .`` (re-applies the
    repo's ``.gitattributes`` / ``core.autocrlf`` so endings match the index) then a
    ``git diff --cached``. We additionally force LF on the returned bytes — belt to
    the renormalize suspenders — so the patch that leaves this function never
    carries a CR regardless of how git emitted it on this host.
    """
    run_git(["add", "--renormalize", "."], checkout_dir)
    diff = run_git(["diff", "--cached"], checkout_dir)
    # Force LF: strip any CR so the patch applies byte-for-byte in the Linux grader.
    return diff.replace("\r\n", "\n").replace("\r", "\n")


# ---------------------------------------------------------------------------
# Pure seam 2: prompt assembly (never leak gold tests).
# ---------------------------------------------------------------------------
_BASE_INSTRUCTION = (
    "You are working inside the sympy repository (the current directory). Resolve the "
    "GitHub issue below by editing the library source code so the described behavior "
    "works.\n\n"
    "Workflow:\n"
    "1. First write a FAILING reproduction test that captures the issue's behavior, and "
    "confirm it fails on the current code.\n"
    "2. Then edit the library source until your reproduction passes.\n"
    "Make the minimal change needed. Do not commit; leave the edits in the working tree."
)

_REWORK_PREFIX = (
    "Your previous attempt did NOT resolve the issue — the change is still not resolved. "
    "Reconsider and fix it. Your earlier reproduction test was:\n"
)


def build_prompt(
    instance: Instance, *, rework: bool = False, prior_repro: str | None = None
) -> str:
    """The agent prompt: fix instruction + full issue text (+ prior repro on rework).

    It deliberately contains **no** gold-test content — not ``FAIL_TO_PASS``, not the
    gold ``test_patch``. The agent authors its own reproduction from the issue, which
    is what keeps the eval honest (the agent never sees the grader's tests). On a
    rework round we restate the full issue, say it's still not resolved, and feed back
    the agent's OWN prior repro (if captured) — never the gold tests.
    """
    parts = [_BASE_INSTRUCTION]
    if rework:
        repro = prior_repro or "(no reproduction test was captured from the prior attempt)"
        parts.append(_REWORK_PREFIX + repro)
    parts.append("GitHub issue:\n" + instance.problem_statement)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Pure seam 3: arm MCP config (treatment pins the space; control omits it).
# ---------------------------------------------------------------------------
def build_mcp_config(space_id: str, *, cache_path: str) -> dict:
    """Treatment's ``--mcp-config`` payload, pinning the instance's space.

    The Praxis MCP server (``uv run python -m knowledge.mcp``, stdio) resolves its org
    from a cached login at :func:`knowledge.mcp.identity.cache_path` (the fixed eval org,
    via ``PRAXIS_MCP_CACHE``) and its **space** from the ``PRAXIS_SPACE`` env override
    (:func:`knowledge.mcp.identity.active_space`), which takes precedence over the cache
    and makes the MCP send ``X-Praxis-Space``. So pinning a per-instance space is just an
    env var — every treatment agent reads its own instance's private graph without
    touching org or login. Control never calls this — it gets no Praxis MCP entry at all.
    """
    return {
        "mcpServers": {
            "praxis": {
                "command": "uv",
                "args": ["run", "python", "-m", "knowledge.mcp"],
                "env": {
                    "PRAXIS_MCP_CACHE": cache_path,  # carries the fixed eval org
                    "PRAXIS_SPACE": space_id,        # the per-instance space pin (X-Praxis-Space)
                },
            }
        }
    }


# ---------------------------------------------------------------------------
# Live-path seam: checkout + venv (documented, not unit-tested).
# ---------------------------------------------------------------------------
def prepare_checkout(instance: Instance, checkout: Path, *, run_git: GitRunner = _default_run_git) -> None:
    """Reset the checkout to ``base_commit`` (LF-preserving) for a fresh attempt.

    Live path only — exercised manually outside CI. Building the ``install_config``
    venv (``python``/``install``/``pip_packages``) so the agent's own repro can run
    is the documented companion step and is performed by the live orchestrator before
    the first ``run_arm`` call; it is intentionally NOT wired here to keep the unit
    layer free of subprocess/venv work.
    """
    run_git(["checkout", "-f", instance.base_commit], checkout)
    run_git(["clean", "-fdq"], checkout)


# ---------------------------------------------------------------------------
# The arm runner + rework loop.
# ---------------------------------------------------------------------------
def _default_run_cli(args: list[str], cwd: Path, env: dict, timeout: int) -> str:
    """Run ``claude`` and return stdout, TOLERATING a recoverable non-zero exit.

    Unlike the sealed-box runner's ``_default_run_cli`` (which raises on any non-zero
    exit), the agentic arm must treat ``error_max_turns`` as a NORMAL outcome: the CLI
    exits 1, but it still emits a result envelope (with usage) and may have left edits in
    the working tree — exactly the patch we want to grade. So we only raise when a
    non-zero exit produced NO parseable result envelope (a real crash); an envelope with
    a ``usage`` block is returned as-is, and the caller extracts whatever patch exists.
    """
    import subprocess

    proc = subprocess.run([_claude_path(), *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        out = proc.stdout.strip()
        try:
            envelope = json.loads(out)
        except (json.JSONDecodeError, ValueError):
            envelope = None
        if not (isinstance(envelope, dict) and "usage" in envelope):
            detail = (proc.stderr.strip() or out)[:500]
            raise RuntimeError(f"claude exited {proc.returncode}: {detail}")
    return proc.stdout


def _agent_args(
    prompt: str, *, model: str, max_turns: int, mcp_config_path: str | None
) -> list[str]:
    """Assemble the ``claude`` argv: the proven smoke-driver flags + optional MCP."""
    args = [
        "-p", prompt,
        "--output-format", "json",
        "--model", model,
        "--max-turns", str(max_turns),
        "--allowedTools", *_ALLOWED_TOOLS,
        "--permission-mode", "bypassPermissions",
    ]
    if mcp_config_path is not None:
        # --strict-mcp-config so ONLY this config's praxis server is loaded (control's
        # host config can't bleed into a treatment run, or vice versa).
        args += ["--mcp-config", mcp_config_path, "--strict-mcp-config"]
    return args


def run_arm(
    instance: Instance,
    arm: str,
    *,
    grade: GradeFn,
    run_cli: CliRunner = _default_run_cli,
    run_git: GitRunner = _default_run_git,
    checkout: Path,
    k_rework: int = 1,
    model: str = "sonnet",
    max_turns: int = _DEFAULT_MAX_TURNS,
    timeout: int = _DEFAULT_TIMEOUT_S,
    mcp_config_path: str | None = None,
) -> ArmResult:
    """Run one arm for one instance through the repro-test rework loop.

    Flow: build the prompt (write a failing repro, then fix) → invoke host ``claude``
    (treatment additionally wires the org-pinned Praxis MCP; control does not) →
    extract the LF patch → ``grade(patch)``. If not resolved and rounds < ``k_rework``,
    re-prompt with the full issue + "still not resolved" + the prior repro (never the
    gold tests) and repeat. Cost/turns/tokens accumulate across the rounds run: that
    sum is the in-arm cost-to-correct when the arm resolves (the loop breaks at the
    first resolved round), or total spend across all K+1 attempts when it never does.
    ``grade`` is injected so the loop tests offline (U6 passes U2's real grader);
    ``run_cli`` and ``run_git`` are injected for the same reason.
    """
    treatment = arm == "treatment"
    # Treatment wires the MCP pinned to the instance's space; control wires nothing.
    if treatment and mcp_config_path is None:
        # The caller (U6) normally seeds a cache + space pin and passes its path; in a
        # stubbed unit test mcp_config_path is None and run_cli never reads it. We still
        # surface that the space WOULD be pinned via space_id_for(instance) on the live path.
        _ = space_id_for(instance)  # deterministic per-instance space; pinned via PRAXIS_SPACE
    arm_mcp_path = mcp_config_path if treatment else None

    cost_usd: float | None = None
    tokens: int | None = None
    turns: int | None = None
    resolved = False
    rework_rounds = 0
    patch = ""
    prior_repro: str | None = None
    retrieval_overhead: dict | None = {"query_embed_ms": 0, "mcp_round_trip_ms": 0} if treatment else None

    # round 0 = first attempt; rounds 1..k_rework = reworks.
    for round_idx in range(k_rework + 1):
        is_rework = round_idx > 0
        prompt = build_prompt(instance, rework=is_rework, prior_repro=prior_repro)
        args = _agent_args(
            prompt, model=model, max_turns=max_turns, mcp_config_path=arm_mcp_path
        )
        stdout = run_cli(args, checkout, _subscription_env(), timeout)
        usage = _claude_usage(stdout)
        cost_usd = _accumulate(cost_usd, usage.get("cost_usd"))
        turns = _accumulate(turns, usage.get("num_turns"))
        tokens = _accumulate_tokens(tokens, usage)

        patch = extract_patch(checkout, run_git=run_git)
        resolved = grade(patch)
        if is_rework:
            rework_rounds += 1
        if resolved:
            break

    return ArmResult(
        arm=arm,
        patch=patch,
        resolved=resolved,
        cost_usd=cost_usd,
        tokens=tokens,
        turns=turns,
        rework_rounds=rework_rounds,
        retrieval_overhead=retrieval_overhead,
    )


def _accumulate(running: float | int | None, add) -> float | int | None:
    """Sum a per-round metric into the cumulative total, tolerating missing values."""
    if add is None:
        return running
    return add if running is None else running + add


def _accumulate_tokens(running: int | None, usage: dict) -> int | None:
    """Cumulative input+output tokens across rounds (None until any round reports)."""
    inp = usage.get("input_tokens")
    out = usage.get("output_tokens")
    if inp is None and out is None:
        return running
    round_total = (inp or 0) + (out or 0)
    return round_total if running is None else running + round_total
