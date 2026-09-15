"""AF_GROK_EFFORT reaches both the Grok round launch and the backend probe, and is optional."""

from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh").read_text()


def test_effort_flag_is_built_only_when_set():
    assert 'local grok_effort_flag=""' in SRC
    assert '[ -n "${AF_GROK_EFFORT:-}" ] && grok_effort_flag="--reasoning-effort ${AF_GROK_EFFORT}"' in SRC


def test_round_launch_and_probe_both_carry_the_effort():
    launch = next(line for line in SRC.splitlines() if line.strip().startswith('CLAUDE_LAUNCH="unset XAI_API_KEY'))
    probe = next(line for line in SRC.splitlines() if "af_probe_generation grok" in line)
    assert "--model ${AF_GROK_MODEL} ${grok_effort_flag} --always-approve" in launch
    assert "--model '$AF_GROK_MODEL' ${grok_effort_flag} --always-approve" in probe


def test_backend_note_names_the_effort():
    assert "${AF_GROK_EFFORT:+ effort=${AF_GROK_EFFORT}}" in SRC
