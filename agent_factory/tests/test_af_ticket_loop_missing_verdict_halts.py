"""A round without a verification verdict must never dispatch its successor."""

from __future__ import annotations

from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"


def test_missing_verdict_returns_a_hard_failure_not_success() -> None:
    src = SCRIPT.read_text()
    start = src.index('if [ ! -f "$VERDICT" ]; then')
    end = src.index("\n  fi", start)
    block = src[start:end]

    assert "no later round will be dispatched" in block
    assert "return 9" in block
    assert "return 0" not in block


def test_verification_cannot_be_disabled_to_reintroduce_fail_open() -> None:
    src = SCRIPT.read_text()
    assert 'AF_VERIFY_ROUND=${AF_VERIFY_ROUND} is forbidden' in src
    assert '[ "${AF_VERIFY_ROUND:-1}" = "1" ] || af_guard_die' in src
    assert '&& [ "${AF_VERIFY_ROUND:-1}" = "1" ]' not in src


def test_incoherent_or_misattributed_verdict_halts_before_regression() -> None:
    src = SCRIPT.read_text()
    halt = src.index("No regression is applied from an incoherent or misattributed verdict")
    apply_regressions = src.index("# THE LOOP performs the regression")
    assert halt < apply_regressions
    assert "return 9" in src[halt:apply_regressions]


def test_unanswerable_post_round_tally_halts_instead_of_skipping_verification() -> None:
    src = SCRIPT.read_text()
    start = src.index("if ! after=$(praxis_q finished_count); then")
    end = src.index('\n  if [ "$after" -gt "$before" ]; then', start)
    block = src[start:end]

    assert "No successor round will be dispatched" in block
    assert "exit 9" in block
    assert 'after=""' not in block
