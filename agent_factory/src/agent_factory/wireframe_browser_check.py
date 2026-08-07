"""Browser-RENDERED wireframe conformance: the decisive gate behind the static parser.

:mod:`agent_factory.wireframe_conformance` proves markup structure cheaply; this module proves the
page actually LOOKS like a designed screen. Both the wireframe and the built page are HTML, so both
are rendered in the SAME headless Chromium (Playwright) and their computed reality is compared:

* computed style — a page with no stylesheet renders in the browser's default serif with zero
  flex/grid containers, trivially distinguishable from the wireframe's styled shell;
* layout geometry — a visible sidebar (nav) with non-zero width left of the main column, a header
  above it, wherever the wireframe has them;
* the interactive control inventory as RENDERED AND VISIBLE, not merely present in markup;
* tap-target sizes, where a ticket asserts them (``--min-tap-px 44``) — a browser measures what a
  grep cannot.

EVIDENCE IS MANDATORY, in the ``gate-integrity`` spirit of ``seeded_checks.toml`` ("an aborted
checker reporting few errors must never score as clean"): the check writes full-page screenshots of
BOTH renders plus ``measurements.json`` and a side-by-side ``compare.html`` into ``--artifact-dir``
and verifies they exist and are non-empty — a run that produced no rendering evidence FAILS, it
never passes. Likewise Playwright or its browser being missing is a LOUD failure (exit 2, with the
remediation printed), never a skip and never a silent degrade to the parser: a browser check that
no-ops converts "unverified" into "verified", which is the exact failure class this exists to stop.

Bundling: the ``playwright`` package is the ``browser`` optional-dependency group of
``agent_factory/pyproject.toml`` (``uv sync --extra browser`` / ``pip install 'agent-factory[browser]'``
— the ~150 MB Chromium download is NOT imposed on every factory install). The browser binary itself
is bootstrapped idempotently on first use: if Chromium is absent this module runs
``python -m playwright install chromium`` once (headless-capable, works on the EC2 devbox) and
retries; if that also fails it exits 2 with the command to run.

CLI::

    python -m agent_factory.wireframe_browser_check --wireframe docs/wireframes/operator.html \
        --screen s-map --url http://localhost:8000/map \
        --artifact-dir .af-artifacts/wireframe-conformance/s-map [--min-tap-px 44]

Exit codes: 0 conformant (artifacts written and referenced in output), 1 violations or missing
evidence, 2 the browser stack is unavailable.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Chromium's default body font resolves to a Times-family serif on every platform; a built page
# still rendering in it has no page-level typography at all.
_DEFAULT_FONT_MARKER = "times"

_MEASURE_JS = """
() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const rect = el => { const r = el.getBoundingClientRect();
    return {x: r.x, y: r.y, w: r.width, h: r.height}; };
  const roleOf = {nav: 'navigation', header: 'banner', main: 'main'};
  const firstVisible = sel => {
    for (const el of document.querySelectorAll(sel)) if (vis(el)) return el; return null; };
  const shell = {};
  for (const part of ['nav', 'header', 'main']) {
    const el = firstVisible(part) || firstVisible(`[role="${roleOf[part]}"]`);
    shell[part] = el ? rect(el) : null;
  }
  let flexGrid = 0; const controls = {}; const taps = [];
  for (const el of document.querySelectorAll('*')) {
    if (!vis(el)) continue;
    const d = getComputedStyle(el).display;
    if (d.includes('flex') || d.includes('grid')) flexGrid++;
    const t = el.tagName.toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();
    let kind = null;
    if (['tab', 'switch', 'button', 'checkbox', 'radio'].includes(role)) kind = 'role=' + role;
    else if (t === 'input') kind = 'input[' + (el.type || 'text') + ']';
    else if (['button', 'select', 'textarea', 'summary', 'a'].includes(t)) kind = t;
    if (kind) { controls[kind] = (controls[kind] || 0) + 1;
      const r = el.getBoundingClientRect();
      taps.push({kind: kind, w: Math.round(r.width), h: Math.round(r.height)}); }
  }
  const bs = getComputedStyle(document.body);
  return {bodyFont: bs.fontFamily, flexGrid: flexGrid, shell: shell, controls: controls,
          tapTargets: taps};
}
"""

# Reveal one wireframe screen: the af-wireframe convention hides sections behind a script-toggled
# class; flipping it mechanically shows the screen under test without simulating clicks.
_REVEAL_JS = """
(screen) => {
  for (const el of document.querySelectorAll('[id^="s-"]')) {
    if (el.id === screen) el.classList.remove('hidden');
    else if (el.id.startsWith('s-')) el.classList.add('hidden');
  }
}
"""


def _fail_loud(msg: str) -> None:
    """Exit 2 (infra failure) with the remediation on stderr — the one and only unavailable path."""
    print(msg, file=sys.stderr)
    raise SystemExit(2)


def ensure_playwright(auto_install: bool = True):
    """Return the ``sync_playwright`` entrypoint, bootstrapping the Chromium binary if absent.

    NEVER degrades quietly: a missing package or browser exits 2 with the exact remediation on
    stderr, because a silently no-oping browser check would certify unverified UI as verified.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _fail_loud(
            "wireframe-browser-check: FAILED — the 'playwright' package is not installed.\n"
            "  Remediation: uv sync --extra browser   (or: pip install 'agent-factory[browser]')\n"
            "  then:        python -m playwright install chromium\n"
            "This check never skips: an absent browser is a failed check, not a pass."
        )
    if _chromium_ready(sync_playwright):
        return sync_playwright
    if auto_install:  # idempotent first-use bootstrap; a present browser makes this a no-op
        print("wireframe-browser-check: Chromium missing; running "
              "'python -m playwright install chromium' (one-time, ~150 MB) …", file=sys.stderr)
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
        if _chromium_ready(sync_playwright):
            return sync_playwright
    _fail_loud(
        "wireframe-browser-check: FAILED — Playwright is installed but its Chromium browser is "
        "not.\n  Remediation: python -m playwright install chromium\n"
        "This check never skips: an absent browser is a failed check, not a pass."
    )


def _chromium_ready(sync_playwright) -> bool:
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:  # noqa: BLE001 - any launch failure means "not ready"; caller stays loud
        return False


def compare_measurements(wf: dict, built: dict, min_tap_px: int | None = None) -> list[str]:
    """PURE comparison of two rendered-measurement dicts (:data:`_MEASURE_JS` output) —
    every way the built page fails to be the designed screen, [] when conformant."""
    v: list[str] = []
    wf_font = str(wf.get("bodyFont") or "").lower()
    font = str(built.get("bodyFont") or "").lower()
    # Comparative, not absolute: only a wireframe that itself sets typography obliges the build to.
    if _DEFAULT_FONT_MARKER not in wf_font and (not font or _DEFAULT_FONT_MARKER in font):
        v.append(f"body renders in the browser default serif ({built.get('bodyFont')!r}) — "
                 f"no page-level typography; wireframe uses {wf.get('bodyFont')!r}")
    if (wf.get("flexGrid") or 0) > 0 and (built.get("flexGrid") or 0) == 0:
        v.append(f"wireframe lays out with {wf['flexGrid']} flex/grid containers; the built page "
                 f"renders ZERO — bare document flow, not the specified shell")

    wf_shell, built_shell = wf.get("shell") or {}, built.get("shell") or {}
    for part in ("nav", "header", "main"):
        if wf_shell.get(part) and not built_shell.get(part):
            v.append(f"wireframe has a visible <{part}> (or its ARIA role); the built page "
                     f"renders none")
    wn, wh, wm = wf_shell.get("nav"), wf_shell.get("header"), wf_shell.get("main")
    bn, bh, bm = built_shell.get("nav"), built_shell.get("header"), built_shell.get("main")
    if wh and wm and bh and bm and wh["y"] <= wm["y"] + 2 and bh["y"] > bm["y"] + 2:
        v.append("wireframe places the header above the main column; the built page does not")
    if wn and wm and bn and bm and wn["x"] < wm["x"] and wn["w"] < wm["w"]:
        if not (bn["x"] < bm["x"] and bn["w"] > 0):
            v.append("wireframe has a sidebar nav left of the main column; the built page's nav "
                     "is not a left sidebar (zero width or not left of main)")

    wf_controls, built_controls = wf.get("controls") or {}, built.get("controls") or {}
    for kind, count in sorted(wf_controls.items()):
        have = built_controls.get(kind, 0)
        if have < count:
            v.append(f"rendered-and-visible control inventory short: wireframe shows {count} x "
                     f"{kind}, built page shows {have}")

    if min_tap_px:
        small = [t for t in (built.get("tapTargets") or [])
                 if min(t.get("w", 0), t.get("h", 0)) < min_tap_px]
        if small:
            worst = sorted(small, key=lambda t: min(t["w"], t["h"]))[:5]
            v.append(f"{len(small)} visible interactive targets are under {min_tap_px}px "
                     f"(worst: {worst})")
    return v


def verify_artifacts(paths: list[Path]) -> list[str]:
    """A run that produced no rendering evidence is a FAILURE, not a pass."""
    return [f"missing or empty evidence artifact: {p}" for p in paths
            if not p.is_file() or p.stat().st_size == 0]


_COMPARE_HTML = """<!doctype html><meta charset="utf-8"><title>wireframe vs built — {screen}</title>
<style>body{{margin:0;font:13px system-ui;background:#111;color:#eee}}
.cols{{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:8px}}
h2{{font-size:13px;margin:6px 8px}}img{{width:100%;border:1px solid #444}}</style>
<div class="cols"><div><h2>wireframe: {screen}</h2><img src="wireframe.png"></div>
<div><h2>built: {url}</h2><img src="built.png"></div></div>
"""


def run_check(wireframe: str, screen: str, url: str, artifact_dir: str,
              min_tap_px: int | None = None) -> int:
    sync_playwright = ensure_playwright()
    art = Path(artifact_dir)
    art.mkdir(parents=True, exist_ok=True)
    wf_png, built_png = art / "wireframe.png", art / "built.png"

    wf_path = Path(wireframe).resolve()
    if not wf_path.is_file():
        print(f"wireframe-browser-check: cannot run: no wireframe at {wf_path}", file=sys.stderr)
        return 2

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        page.goto(wf_path.as_uri(), wait_until="load")
        page.evaluate(_REVEAL_JS, screen)
        wf_measures = page.evaluate(_MEASURE_JS)
        page.screenshot(path=str(wf_png), full_page=True)

        try:
            page.goto(url, wait_until="load", timeout=15000)
        except Exception as exc:  # noqa: BLE001 - unreachable target is a loud infra failure
            browser.close()
            print(f"wireframe-browser-check: cannot run: built page {url} did not load: {exc}",
                  file=sys.stderr)
            return 2
        built_measures = page.evaluate(_MEASURE_JS)
        page.screenshot(path=str(built_png), full_page=True)
        browser.close()

    (art / "measurements.json").write_text(
        json.dumps({"screen": screen, "url": url, "wireframe": wf_measures,
                    "built": built_measures}, indent=2), encoding="utf-8")
    (art / "compare.html").write_text(_COMPARE_HTML.format(screen=screen, url=url),
                                      encoding="utf-8")

    violations = compare_measurements(wf_measures, built_measures, min_tap_px=min_tap_px)
    violations += verify_artifacts([wf_png, built_png, art / "measurements.json"])

    for v in violations:
        print(f"VIOLATION [{screen}]: {v}")
    verdict = "FAIL" if violations else "OK"
    print(f"{verdict} [{screen}]: rendered in Chromium; evidence: {wf_png}, {built_png}, "
          f"{art / 'compare.html'} (side-by-side), {art / 'measurements.json'}")
    return 1 if violations else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wireframe_browser_check", description=__doc__.split("\n")[0])
    ap.add_argument("--wireframe", required=True)
    ap.add_argument("--screen", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--artifact-dir", required=True)
    ap.add_argument("--min-tap-px", type=int, default=None,
                    help="assert every visible interactive target is at least this many px")
    a = ap.parse_args(argv)
    return run_check(a.wireframe, a.screen, a.url, a.artifact_dir, min_tap_px=a.min_tap_px)


if __name__ == "__main__":
    raise SystemExit(main())
