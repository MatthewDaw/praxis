"""`--check` must WITNESS A GENERATION, not merely observe the absence of an auth error.

The defect these tests pin, measured on 2026-08-24: `af-ticket-loop.sh --check` printed
`preflight: OK` for two backends that could not emit a single token.

  * grok     -- sat behind a subscription QUOTA WALL. The probe ran, produced no PONG and no
                *auth* error, and the driver downgraded that to a WARNING and passed.
  * deepseek -- reported "Insufficient Balance". It passed because the DeepSeek branch had no
                probe at all: its entire preflight was "is ~/.deepseek_key non-empty".

A run then launched on a dead backend, every session died on its first turn, and the operator was
told "builds are running". Auth is not quota; a credential that authenticates and cannot spend is
useless to a build.

These are BEHAVIOURAL tests: they execute the shipped driver against stub backend CLIs on PATH and
assert on its exit status. A grep-the-source test would have passed happily against the broken
driver, because the broken driver's probe code looked perfectly reasonable -- the bug was in what
it did with a probe that came back empty-handed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"

# The driver's documented exit codes. Preflight must not collapse these: the remedy for each is a
# different job, and "1" for all three is why the last false OK took an operator to diagnose.
EXIT_PREFLIGHT = 1   # fix the config / log in again
EXIT_CREDIT = 3      # top up the balance
EXIT_QUOTA = 8       # wait for the usage window to reset


def _stub(bin_dir: Path, name: str, transcript: str, *, exit_code: int = 0) -> Path:
    """A fake backend CLI that ignores its arguments and replays one recorded transcript."""
    path = bin_dir / name
    path.write_text(
        "#!/usr/bin/env bash\n"
        f"cat <<'AF_STUB_EOF'\n{transcript}\nAF_STUB_EOF\n"
        f"exit {exit_code}\n"
    )
    path.chmod(0o755)
    return path


def _check(tmp_path: Path, backend: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
    """Run the REAL `--check` entry point in a sealed HOME with a stub CLI on PATH."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)

    env = {
        # A hermetic HOME: no real credential, no ~/.af-backend marker, and -- crucially -- no way
        # for the test to spend the operator's actual quota.
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TMPDIR": str(tmp_path / "tmp"),
        "AF_MODEL_BACKEND": backend,
        # The retry exists for a network blip, not for a test's wall clock.
        "AF_PROBE_RETRY_S": "0",
    }
    (tmp_path / "tmp").mkdir(exist_ok=True)
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT), "--check"],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


# --------------------------------------------------------------------------- the two real ones --

def test_deepseek_insufficient_balance_refuses_the_run(tmp_path: Path):
    """THE REGRESSION. A funded-looking key file plus an empty account used to preflight OK."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    _stub(bin_dir, "claude", "API Error: 402 {\"error\":{\"message\":\"Insufficient Balance\"}}")
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    # The key file is present and non-empty -- i.e. it satisfies the ENTIRE old preflight.
    (home / ".deepseek_key").write_text("sk-not-a-real-key\n")

    res = _check(tmp_path, "deepseek")

    assert res.returncode == EXIT_CREDIT, (
        "an exhausted DeepSeek balance must refuse the run with the billing exit code, "
        f"got {res.returncode}\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    assert "preflight   : OK" not in res.stdout
    assert "CANNOT SPEND" in res.stderr


def test_grok_quota_wall_refuses_the_run(tmp_path: Path):
    """A quota wall answers with no token and no auth error -- the shape that used to pass."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "home"
    (home / ".grok" / "bin").mkdir(parents=True, exist_ok=True)
    (home / ".grok" / "auth.json").write_text('{"oauth": {"access_token": "stub"}}')
    grok = _stub(
        home / ".grok" / "bin",
        "grok",
        '{"error":"You have hit your usage limit reached for grok-4.6. '
        'Your limit will reset in 3 hours."}',
    )

    res = _check(tmp_path, "grok", GROK_BIN=str(grok))

    assert res.returncode == EXIT_QUOTA, (
        "a spent grok allowance must refuse the run with the quota exit code, "
        f"got {res.returncode}\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    assert "preflight   : OK" not in res.stdout
    assert "ALLOWANCE IS SPENT" in res.stderr


# ------------------------------------------------------------- the contract around those two ----

def test_a_healthy_backend_still_passes_and_says_what_it_proved(tmp_path: Path):
    """The gate has to stay passable, and it has to report the WITNESS, not a bare 'OK'."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    _stub(bin_dir, "claude", "PONG")
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".deepseek_key").write_text("sk-not-a-real-key\n")

    res = _check(tmp_path, "deepseek")

    assert res.returncode == 0, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    assert "preflight   : OK" in res.stdout
    assert "generation  : WITNESSED" in res.stdout


def test_silence_is_not_success(tmp_path: Path):
    """A backend that prints NOTHING is exactly as unusable as one that errors.

    Installed plugins mute headless `claude -p`, so 'exited 0, said nothing' is a real shape the
    driver sees -- and treating it as OK is indistinguishable from the bug being fixed here.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    _stub(bin_dir, "claude", "")
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".deepseek_key").write_text("sk-not-a-real-key\n")

    res = _check(tmp_path, "deepseek")

    assert res.returncode == EXIT_PREFLIGHT
    assert "could not witness a generation" in res.stderr


def test_lenient_escape_hatch_covers_silence_but_never_an_exhausted_account(tmp_path: Path):
    """AF_PROBE_LENIENT is for an offline box, not for spending money that is not there.

    The distinction is the point: an inconclusive probe is ABSENCE OF PROOF, which an operator may
    knowingly accept. 'Insufficient Balance' is PROOF OF FAILURE, which no flag may wave through --
    otherwise the flag just reintroduces the defect under a new name.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".deepseek_key").write_text("sk-not-a-real-key\n")

    _stub(bin_dir, "claude", "")
    silent = _check(tmp_path, "deepseek", AF_PROBE_LENIENT="1")
    assert silent.returncode == 0, "an operator may accept an unproven backend"
    assert "UNPROVEN" in silent.stdout or "UNPROVEN" in silent.stderr

    _stub(bin_dir, "claude", "API Error: 402 Insufficient Balance")
    broke = _check(tmp_path, "deepseek", AF_PROBE_LENIENT="1")
    assert broke.returncode == EXIT_CREDIT, (
        "AF_PROBE_LENIENT must NOT be able to wave through a proven-dead account"
    )


def test_the_prompt_echo_cannot_be_mistaken_for_the_answer(tmp_path: Path):
    """`codex exec` replays the user prompt, which contains the literal word PONG.

    A probe that greps the whole transcript therefore 'passes' without the model ever answering --
    the same false OK, arrived at from the other direction.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".deepseek_key").write_text("sk-not-a-real-key\n")
    _stub(bin_dir, "claude", "user instructions:\nReply with exactly: PONG\n[stream error]")

    res = _check(tmp_path, "deepseek")

    assert res.returncode == EXIT_PREFLIGHT, (
        "the echoed prompt is not a generation; got a pass on the echo alone"
    )
