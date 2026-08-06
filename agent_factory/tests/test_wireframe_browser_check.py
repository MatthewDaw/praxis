"""The browser-rendered wireframe gate: pure comparison logic, MANDATORY evidence, and — the part
that matters most — fail-LOUD when the browser stack is absent. A browser check that silently
no-ops converts "unverified" into "verified"; these tests pin that it cannot."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agent_factory.wireframe_browser_check import compare_measurements, verify_artifacts

_SRC = str(Path(__file__).resolve().parents[1] / "src")


def _rect(x=0, y=0, w=200, h=800):
    return {"x": x, "y": y, "w": w, "h": h}


def _styled(**over):
    m = {
        "bodyFont": 'ui-sans-serif, system-ui, "Segoe UI", sans-serif',
        "flexGrid": 14,
        "shell": {"nav": _rect(0, 0, 216, 900), "header": _rect(216, 0, 1000, 60),
                  "main": _rect(216, 60, 1000, 800)},
        "controls": {"button": 6, "select": 2, "input[checkbox]": 5},
        "tapTargets": [{"kind": "button", "w": 120, "h": 44}],
    }
    m.update(over)
    return m


_UNSTYLED = {
    "bodyFont": "Times",   # Chromium's default serif — the fingerprint of zero CSS
    "flexGrid": 0,
    "shell": {"nav": None, "header": None, "main": None},
    "controls": {},
    "tapTargets": [{"kind": "a", "w": 40, "h": 17}],
}


# ------------------------------------------------------------------- pure comparison

def test_unstyled_page_fails_on_every_rendered_axis():
    text = "\n".join(compare_measurements(_styled(), _UNSTYLED))
    assert "default serif" in text
    assert "ZERO" in text                      # no flex/grid containers
    assert "<nav>" in text and "<header>" in text and "<main>" in text
    assert "control inventory short" in text


def test_faithful_render_passes():
    assert compare_measurements(_styled(), _styled()) == []


def test_sidebar_geometry_is_compared_not_just_presence():
    built = _styled()
    # nav exists but with zero width and not left of main — still a violation.
    built["shell"] = {"nav": _rect(600, 60, 0, 0), "header": _rect(0, 0, 1200, 60),
                      "main": _rect(0, 60, 1200, 800)}
    assert any("sidebar" in v for v in compare_measurements(_styled(), built))


def test_header_must_sit_above_main_when_the_wireframe_says_so():
    built = _styled()
    built["shell"] = dict(built["shell"], header=_rect(0, 900, 1200, 60), main=_rect(0, 0, 1200, 800))
    assert any("header above the main" in v for v in compare_measurements(_styled(), built))


def test_tap_targets_measured_only_when_asserted():
    built = _styled(tapTargets=[{"kind": "button", "w": 120, "h": 30}])
    assert compare_measurements(_styled(), built) == []                      # not asserted
    assert any("under 44px" in v
               for v in compare_measurements(_styled(), built, min_tap_px=44))  # asserted


# ------------------------------------------------------------------- evidence is mandatory

def test_missing_or_empty_artifact_is_a_failure(tmp_path):
    real = tmp_path / "built.png"
    real.write_bytes(b"\x89PNG data")
    empty = tmp_path / "wireframe.png"
    empty.touch()
    missing = tmp_path / "measurements.json"
    problems = verify_artifacts([real, empty, missing])
    assert len(problems) == 2
    assert all("evidence artifact" in p for p in problems)


# ------------------------------------------------------------------- fail-loud, never skip

def _run_module(extra_env_code: str, *args: str) -> subprocess.CompletedProcess:
    """Run the module in a subprocess with an import shim prepended via sitecustomize-style -c."""
    code = (extra_env_code +
            "\nimport runpy, sys; sys.argv = ['wireframe_browser_check'] + " + repr(list(args)) +
            "\nrunpy.run_module('agent_factory.wireframe_browser_check', run_name='__main__')")
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=_SRC)


def test_missing_playwright_package_fails_loud_with_remediation(tmp_path):
    shim = ("import sys\n"
            "sys.modules['playwright'] = None\n"
            "sys.modules['playwright.sync_api'] = None\n")
    proc = _run_module(shim, "--wireframe", "x.html", "--screen", "s-map",
                       "--url", "http://localhost:1/x", "--artifact-dir", str(tmp_path))
    assert proc.returncode == 2
    out = proc.stdout + proc.stderr
    assert "FAILED" in out and "playwright install chromium" in out
    assert "never skips" in out


def test_ensure_playwright_never_returns_none(monkeypatch, capsys):
    """The API contract: ensure_playwright either returns a working entrypoint or raises
    SystemExit(2) — there is no quiet None/skip path a caller could mistake for 'pass'."""
    import agent_factory.wireframe_browser_check as m
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(SystemExit) as exc:
        m.ensure_playwright(auto_install=False)
    assert exc.value.code == 2
    assert "never skips" in capsys.readouterr().err


# ------------------------------------------------------------------- real browser (opt-in)

def _playwright_ready() -> bool:
    try:
        import agent_factory.wireframe_browser_check as m
        from playwright.sync_api import sync_playwright
        return m._chromium_ready(sync_playwright)
    except Exception:  # noqa: BLE001 - the e2e is optional; the fail-loud tests above are not
        return False


@pytest.mark.skipif(not _playwright_ready(), reason="playwright+chromium not installed here "
                    "(the CHECK itself never skips — exit 2; only this optional e2e does)")
def test_end_to_end_unstyled_page_fails_and_evidence_is_written(tmp_path):
    import agent_factory.wireframe_browser_check as m
    from test_wireframe_conformance import STUB, WIREFRAME

    wf = tmp_path / "wire.html"
    wf.write_text(WIREFRAME, encoding="utf-8")
    stub = tmp_path / "stub.html"
    stub.write_text(STUB, encoding="utf-8")
    art = tmp_path / "art"

    rc = m.run_check(str(wf), "s-map", stub.resolve().as_uri(), str(art))
    assert rc == 1
    for name in ("wireframe.png", "built.png", "measurements.json", "compare.html"):
        p = art / name
        assert p.is_file() and p.stat().st_size > 0
