"""The grok backend must be an explicit, exclusive subscription path.

These tests read the shipped driver. A comment or a helper that is never
called does not count — the bytes `resolve_backend` actually runs have to
accept `grok`, refuse to spend XAI_API_KEY, default the model to grok-4.6,
and keep the unknown-backend fallback on deepseek (never sonnet).
"""

from __future__ import annotations

from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"


def _text() -> str:
    return SCRIPT.read_text()


def test_grok_is_an_accepted_backend():
    text = _text()
    assert "sonnet|deepseek|grok" in text
    assert 'BACKEND" = "grok"' in text or "BACKEND\" = \"grok\"" in text


def test_grok_launch_unsets_api_keys_and_pins_latest_model():
    text = _text()
    launch = next(
        line
        for line in text.splitlines()
        if "GROK_BIN" in line and "--always-approve" in line and "CLAUDE_LAUNCH=" in line
    )
    assert "unset XAI_API_KEY" in launch
    assert "GROK_CODE_XAI_API_KEY" in launch
    assert "--model ${AF_GROK_MODEL}" in launch or "--model ${AF_GROK_MODEL:-grok-4.6}" in launch
    assert "AF_GROK_MODEL=\"${AF_GROK_MODEL:-grok-4.6}\"" in text


def test_unknown_backend_still_falls_back_to_deepseek():
    text = _text()
    assert "falling back to deepseek (never to a paid subscription)" in text
    # A typo of grok must not become sonnet.
    assert 'BACKEND="deepseek"' in text
