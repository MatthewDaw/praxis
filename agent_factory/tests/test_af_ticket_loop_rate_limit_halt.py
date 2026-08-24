"""B-1: a headless verify/build session must NEVER silently block on an interactive prompt.

When the Claude Max subscription hits its session/usage limit the CLI does not error -- it strands
the headless session on the interactive `/rate-limit-options` menu, which nothing can answer.
Observed 2026-08-10 on the taolu-coach remote build: round #3's verify session sat on that menu,
burned the full 15-min stall window, and produced an UNVERIFIED round; rounds #4 and #5 then walked
their workers into the identical wall and finished ZERO tickets, caught only by the generic
pane-unchanged watchdog.

These tests do not re-implement the driver's logic. They SLICE the shipped `rate_limited` /
`halt_quota_blocked` functions out of `af-ticket-loop.sh` and execute them against stubbed
collaborators (`say`, `tmux`), and they assert -- on the shipped bytes -- that the rate-limit guard
is wired into the verify and build wait loops AHEAD of the generic stall break, so the loop reacts
the instant the menu appears instead of waiting the window out.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"


def _script_text() -> str:
    return SCRIPT.read_text()


# The real menu text, as captured in the loop log's dumped verify pane.
RATE_LIMIT_PANE = """\
● Running the full test suite…

You've hit your session limit · resets 11:50pm (UTC)

/rate-limit-options
  1. Stop and wait for limit to reset
  2. Switch to usage credits
  3. Switch to Team plan
"""

# Ordinary output that MENTIONS rate/limit/reset words but is NOT the interactive menu. A bare
# "rate limit" grep would false-positive on all of this and kill healthy sessions; the shipped
# detector keys on the menu's own strings, so this must read CLEAR.
BENIGN_PANE = """\
test_rate_limiter.py::test_resets_counter PASSED
  the upstream API rate limit is 100 req/s and the window resets every minute
running: pytest -k "reset" tests/ratelimit/
  429 handled; backing off and retrying
"""


def _slice(pattern: str, *, flags: int = 0) -> str:
    hit = re.search(pattern, _script_text(), flags)
    assert hit, f"could not slice {pattern!r} out of the shipped driver"
    return hit.group(0)


def _funcs() -> str:
    """The shipped rate-limit detector + halt helper + exit-code constant, verbatim."""
    return "\n".join((
        # The matchers rate_limited is built on. Absent, it is a silent 127 that reads as "no
        # match" -- i.e. the detector under test would report every pane benign.
        *re.findall(r"^af_(?:i?has|hasf|hasx)\(\)\{.*$", _script_text(), re.M),
        _slice(r"^AF_EXIT_QUOTA_BLOCKED=\d+", flags=re.M),
        _slice(r"^rate_limited\(\)\{[^\n]*\}", flags=re.M),
        _slice(r"^halt_quota_blocked\(\)\{.*?^\}", flags=re.S | re.M),
    ))


def _run(snippet: str, tmp_path: Path, *, stdin: str = "") -> tuple[int, str]:
    """Source the sliced functions with stubbed collaborators, run `snippet`, return (rc, log)."""
    log = tmp_path / "loop.log"
    harness = tmp_path / "harness.sh"
    harness.write_text("\n".join([
        "#!/usr/bin/env bash",
        "set -uo pipefail",
        f'LOG="{log}"',
        'BACKEND="sonnet"',
        'BACKEND_NOTE="Anthropic subscription (Claude max), model=sonnet — spends Claude quota, NOT API credits"',
        'say(){ echo "$*" >> "$LOG"; }',
        'tmux(){ echo "STUB tmux $*" >> "$LOG"; }',
        'commit_wip(){ echo "STUB commit_wip" >> "$LOG"; }',
        _funcs(),
        snippet,
        "",
    ]))
    out = subprocess.run(["/usr/bin/env", "bash", str(harness)], input=stdin,
                         capture_output=True, text=True, cwd=str(tmp_path))
    return out.returncode, (log.read_text() if log.exists() else "")


# ---------------------------------------------------------------- the detector


def test_rate_limit_menu_is_detected(tmp_path):
    # `rate_limited` takes the pane as an ARGUMENT now. It used to read stdin, and the caller
    # spelled that `echo "$pane" | rate_limited` -- a pipeline, under `set -o pipefail`, whose grep
    # exits on first match and SIGPIPEs the writer. The pipeline then reports FAILURE on a
    # successful match, so the menu went undetected precisely when the pane was long enough for
    # grep to finish early. See test_the_detector_still_fires_on_a_long_pane below.
    rc, _ = _run('rate_limited "$(cat)" && echo HIT', tmp_path, stdin=RATE_LIMIT_PANE)
    assert rc == 0, "the interactive /rate-limit-options menu was not detected"


def test_the_detector_still_fires_when_the_menu_is_buried_in_a_long_pane(tmp_path):
    """The regression the argument form exists for. A real pane is tens of kilobytes and the menu
    scrolls UP as output accumulates, so the match is near the TOP -- the exact case where grep -q
    finishes first and the old pipeline reported no match."""
    buried = RATE_LIMIT_PANE + "\n" + ("filler output line, entirely benign\n" * 20000)
    rc, _ = _run('rate_limited "$(cat)" && echo HIT', tmp_path, stdin=buried)
    assert rc == 0, "the menu was missed because it was not the last thing on screen"


def test_ordinary_ratelimit_words_do_not_false_positive(tmp_path):
    """The whole point of keying on the menu's own strings: a bare 'rate limit' match would reap
    healthy sessions whose output merely mentions rate limiting."""
    rc, _ = _run('rate_limited "$(cat)" && echo HIT', tmp_path, stdin=BENIGN_PANE)
    assert rc == 1, "ordinary rate/limit/reset prose was misread as the quota-blocked menu"


# ---------------------------------------------------------------- the halt


def test_halt_exits_with_the_distinct_quota_code_and_a_loud_diagnostic(tmp_path):
    rc, log = _run('halt_quota_blocked "verify round #3" "af-verify"; echo "SURVIVED=$?"',
                   tmp_path)
    assert rc == 8, f"quota halt must exit with the distinct code 8, got {rc}"
    assert "SURVIVED" not in log, "halt_quota_blocked returned instead of halting the run"
    # Loud, and named as QUOTA -- not a generic 'frozen' line and not the API-credit BILLING line.
    assert "QUOTA BLOCKED at verify round #3" in log
    assert "session/usage limit" in log
    assert "/rate-limit-options" in log
    assert "subscription" in log.lower()
    assert "frozen" not in log.lower(), "the quota halt must be DISTINCT from a generic stall"
    assert "BILLING" not in log, "the quota halt must be DISTINCT from the API-credit 402 path"
    # It tears the stranded session down.
    assert "STUB tmux kill-session -t af-verify" in log


def test_the_quota_code_is_distinct_from_the_billing_code():
    const = _slice(r"^AF_EXIT_QUOTA_BLOCKED=(\d+)", flags=re.M)
    code = int(const.split("=", 1)[1])
    assert code == 8
    assert code != 3, "quota-blocked (wait/switch plan) must not collide with billing exit 3 (top up)"


# ---------------------------------------------------------------- wiring: caught BEFORE the stall


def test_verify_wait_checks_rate_limit_before_declaring_a_stall():
    """Requirement (b)/(c): in the verify wait loop the rate-limit guard must run BEFORE the generic
    'verify session frozen … giving up' break, so the loop halts the instant the menu appears
    instead of burning the full 15-min stall window and emitting an UNVERIFIED round."""
    text = _script_text()
    guard = text.index('rate_limited "$pane"; then halt_quota_blocked "verify round #$rnd"')
    frozen = text.index("verify session frozen for")
    assert guard < frozen, "the verify rate-limit guard runs AFTER the stall break — it would never fire in time"


def test_build_wait_checks_rate_limit_before_declaring_a_stall_and_commits_wip():
    text = _script_text()
    guard = text.index('rate_limited "$pane"; then')
    # the BUILD guard is the one that commits WIP first
    build_guard = text.index("commit_wip\n      halt_quota_blocked \"build round #$round\"")
    frozen = text.index("treating as frozen/stalled, ending wait")
    assert build_guard < frozen, "the build rate-limit guard runs AFTER the stall break"
    assert guard <= build_guard


def test_the_invariant_is_encoded_as_a_comment():
    """A constitutional invariant, pinned so a future refactor cannot quietly drop it."""
    text = _script_text()
    assert "CONSTITUTIONAL INVARIANT" in text
    # The phrase wraps across comment lines; collapse comment continuations before matching.
    collapsed = re.sub(r"\n#\s*", " ", text)
    assert "must NEVER silently block on an interactive prompt" in collapsed


def test_subscription_backend_gets_a_preflight_quota_note():
    """(d): on a subscription backend the loop warns at start that a long unattended run can hit the
    session limit and halt.

    This assertion used to read `assert "*subscription*)" in text` — the shape of a case arm that
    matched BACKEND_NOTE's prose. The dispatch was later rewritten to switch on $BACKEND by name,
    which is strictly better (a note is now per-backend, and grok and codex got their own), and the
    test has been red ever since against a feature that works perfectly. A check that can never pass
    is not a check; it is 1/25th of the reason a whole-repo gate regressed a healthy ticket.

    So assert the BEHAVIOUR: every subscription backend announces its billing model at startup, and
    the Claude one specifically warns about the session quota and names the halt code.
    """
    text = _script_text()
    arm = text[text.index('case "$BACKEND" in\n  sonnet) say "NOTE: backend is a'):]
    arm = arm[: arm.index("esac")]
    for backend in ("sonnet", "codex", "grok"):
        assert f"{backend}) say " in arm, f"{backend} announces no billing model at startup"
    assert re.search(r"NOTE: backend is a Claude subscription \(session quota, not API credits\)", arm)
    assert "AF_EXIT_QUOTA_BLOCKED" in arm, "the warning must name the exit code the run will halt with"
