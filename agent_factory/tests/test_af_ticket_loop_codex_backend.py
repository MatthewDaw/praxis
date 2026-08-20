"""The codex backend must be an explicit, exclusive ChatGPT-subscription path.

Same contract the grok backend is held to: the bytes `resolve_backend` actually
runs have to accept `codex`, refuse to spend OPENAI_API_KEY, refuse an API-key
credential outright (the wrong bill), and leave the unknown-backend fallback on
deepseek. The prompt must also reach codex as initial argv rather than being
typed into a TUI that may not be ready.
"""

from __future__ import annotations

from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"


def _text() -> str:
    return SCRIPT.read_text()


def test_codex_is_an_accepted_backend():
    text = _text()
    assert "sonnet|deepseek|grok|codex" in text
    assert 'BACKEND" = "codex"' in text


def test_codex_launch_unsets_api_keys_and_bypasses_approvals():
    text = _text()
    launch = next(
        line
        for line in text.splitlines()
        if "CLAUDE_LAUNCH=" in line and "CODEX_BIN" in line
    )
    assert "unset OPENAI_API_KEY" in launch
    assert "--dangerously-bypass-approvals-and-sandbox" in launch


def test_codex_refuses_an_api_key_credential():
    text = _text()
    assert "codex credential is an API key" in text
    assert '"OPENAI_API_KEY"' in text
    # login status is the authoritative credential check, not a guessed filename.
    assert "login status" in text


def test_codex_missing_credential_names_the_exact_fix():
    text = _text()
    assert "codex login" in text
    assert "Device Code" in text


def test_codex_prompt_is_delivered_as_argv():
    text = _text()
    assert 'af_prompt_is_argv(){ [ "$BACKEND" = grok ] || [ "$BACKEND" = codex ]; }' in text
    assert '[ "$BACKEND" != grok ]' not in text


def test_unknown_backend_still_falls_back_to_deepseek():
    text = _text()
    assert "falling back to deepseek (never to a paid subscription)" in text
