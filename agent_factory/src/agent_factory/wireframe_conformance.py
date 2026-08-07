"""Mechanical wireframe-conformance checks, generated FROM a Praxis surface binding.

The failure this closes: a build agent ships a route whose renderer is literally
``f"<html><head><title>{t}</title></head><body><h1>{h}</h1>{b}</body></html>"`` — zero CSS, no
shell — and every pinned check stays green, because byte floors and string greps are structurally
blind to presentation. The wireframe, however, is already a parseable HTML artifact in the repo,
and every surface binding (``praxis_bind_surface``) carries its ``file`` + ``screen_id`` +
``states``. So the check is DERIVED from the wireframe rather than hand-authored per project:

* the served response must carry a stylesheet whose (comment/whitespace-normalized) size clears a
  floor derived from the wireframe's own ``<style>`` size — never a magic constant;
* the wireframe's STRUCTURAL class inventory for the screen (class tokens used in the screen's
  markup AND styled by the wireframe's own CSS, minus classes its script toggles at runtime) must
  appear in the served markup;
* the binding's declared ``states`` must be renderable — each state token appears as a class,
  ``data-*`` value, or visible text;
* the interactive control inventory (buttons, selects, inputs, links, tabs …) must be present in
  kind and count;
* NO remote asset URL (CDN script, font, external stylesheet/image) may appear — the wireframe is
  self-contained and so must the build be.

This static pass is the CHEAP, FAST gate: it parses markup and proves structure. The decisive gate
is its sibling :mod:`agent_factory.wireframe_browser_check`, which renders both pages in a real
browser and compares computed style + layout geometry; :func:`emit_check_definition` chains the two
into one ``run`` command. Stdlib only (``html.parser``); no Praxis calls — the check DEFINITION this
emits is authored into ``building-validation`` by :func:`agent_factory.ingestion_api.plan_time_author_check`
(the sole writer of that snapshot), never written by this module.

CLI::

    python -m agent_factory.wireframe_conformance check --wireframe docs/wireframes/operator.html \
        --screen s-map --url http://localhost:8000/map --states loading,empty,error
    python -m agent_factory.wireframe_conformance emit --project farm --wireframe ... \
        --screen s-map --url-template http://localhost:8000/{screen}
    python -m agent_factory.wireframe_conformance emit --project farm --wireframe ... \
        --bindings-json bindings.json --url-template http://localhost:8000/{screen}

Exit codes for ``check``: 0 conformant, 1 violations (printed one per line), 2 the check itself
could not run (missing wireframe, unreachable URL) — never a silent pass.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

# Default floor ratio: the wireframe styles every screen plus the shared shell, so a single served
# page legitimately ships somewhat less CSS — but a page with an order of magnitude less has no
# design system. Overridable per check (``--style-floor-ratio``).
DEFAULT_STYLE_FLOOR_RATIO = 0.5

# af-wireframe convention: screens are sections with ids like "s-map". Sibling screens are excluded
# from a screen's structural inventory; everything outside ANY screen root is the shared shell
# (nav/header) and is included.
_SCREEN_ID_PREFIX = "s-"

_CSS_CLASS_RE = re.compile(r"\.([A-Za-z_-][A-Za-z0-9_-]*)")
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)|@import\s+['\"]?([^'\")\s;]+)")
_WS_RE = re.compile(r"\s+")

# Attributes that can carry an asset reference, per tag.
_ASSET_ATTRS = {"script": ("src",), "link": ("href",), "img": ("src", "srcset"),
                "iframe": ("src",), "source": ("src", "srcset"), "video": ("src", "poster"),
                "audio": ("src",), "embed": ("src",), "object": ("data",)}

# Interactive control kinds counted for the inventory. Inputs are keyed by type.
_CONTROL_TAGS = ("button", "select", "textarea", "summary", "a")


@dataclass
class _El:
    tag: str
    attrs: dict
    parent: "_El | None"
    children: list = field(default_factory=list)
    text: str = ""

    @property
    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()


class _Tree(HTMLParser):
    """A minimal DOM: enough structure to scope a screen's subtree and read class/attr inventories."""

    _VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
                       "param", "source", "track", "wbr"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _El("#root", {}, None)
        self._cur = self.root
        self.styles: list[str] = []
        self.scripts: list[str] = []
        self._in: str | None = None

    def handle_starttag(self, tag, attrs):
        el = _El(tag, dict(attrs), self._cur)
        self._cur.children.append(el)
        if tag in ("style", "script"):
            self._in = tag
        if tag not in self._VOID:
            self._cur = el

    def handle_startendtag(self, tag, attrs):
        self._cur.children.append(_El(tag, dict(attrs), self._cur))

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self._in = None
        node = self._cur
        while node is not self.root:
            if node.tag == tag:
                self._cur = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        if self._in == "style":
            self.styles.append(data)
        elif self._in == "script":
            self.scripts.append(data)
        else:
            self._cur.text += data


def _parse(html_text: str) -> _Tree:
    t = _Tree()
    t.feed(html_text)
    return t


def _walk(el: _El):
    for c in el.children:
        yield c
        yield from _walk(c)


def normalized_css_bytes(css: str) -> int:
    """Comment-stripped, whitespace-collapsed byte size — so a minified served stylesheet is
    compared fairly against a readable wireframe one."""
    return len(_WS_RE.sub(" ", _CSS_COMMENT_RE.sub("", css)).strip().encode("utf-8"))


def _styled_classes(css: str) -> set[str]:
    """Class names the stylesheet actually addresses — the definition of 'structural'."""
    return set(_CSS_CLASS_RE.findall(_CSS_COMMENT_RE.sub("", css)))


def _dynamic_classes(styled: set[str], script_text: str) -> set[str]:
    """Classes the wireframe's own script toggles (quoted occurrences) — runtime state like
    ``hidden``/``on``/``show``, mechanically excluded from the must-be-present inventory."""
    return {c for c in styled
            if f"'{c}'" in script_text or f'"{c}"' in script_text or f"`{c}`" in script_text}


def _control_kind(el: _El) -> str | None:
    role = (el.attrs.get("role") or "").strip().lower()
    if role in ("tab", "switch", "button", "checkbox", "radio"):
        return f"role={role}"
    if el.tag == "input":
        return f"input[{(el.attrs.get('type') or 'text').strip().lower()}]"
    if el.tag in _CONTROL_TAGS:
        return el.tag
    return None


def _remote_urls_in_markup(tree: _Tree, own_origin: str | None) -> list[str]:
    found: list[str] = []
    for el in _walk(tree.root):
        for attr in _ASSET_ATTRS.get(el.tag, ()):
            found.extend(_remote(v, own_origin) for v in (el.attrs.get(attr) or "").split(",") if v)
    return [u for u in found if u]


def _remote(url: str, own_origin: str | None) -> str | None:
    u = url.strip().split()[0] if url.strip() else ""
    if not u or u.startswith(("data:", "#", "blob:")):
        return None
    if u.startswith("//"):
        return u
    parsed = urllib.parse.urlparse(u)
    if parsed.scheme in ("http", "https"):
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return None if own_origin and origin == own_origin else u
    return None  # relative / local


def _remote_urls_in_css(css: str, own_origin: str | None) -> list[str]:
    out = []
    for m in _CSS_URL_RE.finditer(_CSS_COMMENT_RE.sub("", css)):
        out.append(_remote(m.group(1) or m.group(2) or "", own_origin))
    return [u for u in out if u]


@dataclass(frozen=True)
class ScreenSpec:
    """Everything the wireframe asserts about one screen, computed — never hand-listed."""

    screen_id: str
    style_bytes: int                 # normalized bytes of the wireframe's own <style>
    structural_classes: frozenset    # classes used by (screen subtree ∪ shell) AND styled by the CSS
    controls: dict                   # kind -> count, in (screen subtree ∪ shell)


def screen_spec(wireframe_html: str, screen_id: str) -> ScreenSpec:
    """Derive the per-screen spec: the screen's own subtree PLUS the shared shell (everything
    outside any sibling screen root, which by the af-wireframe convention is an element whose id
    starts with ``s-``)."""
    tree = _parse(wireframe_html)
    css = "\n".join(tree.styles)
    styled = _styled_classes(css)
    dynamic = _dynamic_classes(styled, "\n".join(tree.scripts))

    def in_scope(el: _El) -> bool:
        node = el
        while node is not None:
            nid = node.attrs.get("id") or ""
            if nid == screen_id:
                return True
            if nid.startswith(_SCREEN_ID_PREFIX) and nid != screen_id:
                return False  # a sibling screen's subtree
            node = node.parent
        return True  # the shared shell

    classes: set[str] = set()
    controls: Counter = Counter()
    seen_screen = False
    for el in _walk(tree.root):
        if (el.attrs.get("id") or "") == screen_id:
            seen_screen = True
        if not in_scope(el):
            continue
        classes.update(el.classes)
        kind = _control_kind(el)
        if kind:
            controls[kind] += 1
    if not seen_screen:
        raise ValueError(f"wireframe has no element with id={screen_id!r}")
    return ScreenSpec(screen_id=screen_id, style_bytes=normalized_css_bytes(css),
                      structural_classes=frozenset((classes & styled) - dynamic),
                      controls=dict(controls))


# --------------------------------------------------------------------------- the served-page audit

def _fetch(url: str, timeout: float = 10.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - localhost check target
        return resp.read().decode("utf-8", errors="replace")


def _served_css(tree: _Tree, base_url: str | None) -> tuple[str, list[str]]:
    """Inline <style> plus every LOCAL linked stylesheet (resolved against ``base_url``).
    Returns (css_text, violations) — a declared-but-unfetchable local stylesheet is a violation,
    not a silent shrink of the floor."""
    css = "\n".join(tree.styles)
    problems: list[str] = []
    for el in _walk(tree.root):
        if el.tag != "link" or "stylesheet" not in (el.attrs.get("rel") or "").lower():
            continue
        href = (el.attrs.get("href") or "").strip()
        if not href or _remote(href, _origin(base_url)):
            continue  # remote links are reported by the remote-asset rule instead
        if not base_url:
            problems.append(f"linked stylesheet {href!r} cannot be resolved without a base URL")
            continue
        try:
            css += "\n" + _fetch(urllib.parse.urljoin(base_url, href))
        except OSError as exc:
            problems.append(f"linked stylesheet {href!r} could not be fetched: {exc}")
    return css, problems


def _origin(url: str | None) -> str | None:
    if not url:
        return None
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}" if p.scheme in ("http", "https") else None


def audit_served(served_html: str, spec: ScreenSpec, states: list[str] | None = None,
                 base_url: str | None = None,
                 style_floor_ratio: float = DEFAULT_STYLE_FLOOR_RATIO) -> list[str]:
    """Every violation of the wireframe-derived contract, [] when conformant. Pure given its
    inputs except that LOCAL linked stylesheets are fetched relative to ``base_url``."""
    tree = _parse(served_html)
    own_origin = _origin(base_url)
    violations: list[str] = []

    css, css_problems = _served_css(tree, base_url)
    violations.extend(css_problems)
    floor = int(spec.style_bytes * style_floor_ratio)
    got = normalized_css_bytes(css)
    if got < floor:
        violations.append(
            f"stylesheet too small: {got} normalized bytes served vs a floor of {floor} "
            f"({style_floor_ratio:.0%} of the wireframe's own {spec.style_bytes}); an unstyled or "
            f"token-styled page is not the specified screen")

    served_classes = {c for el in _walk(tree.root) for c in el.classes}
    missing = sorted(spec.structural_classes - served_classes)
    if missing:
        violations.append(
            f"structural classes from the wireframe absent from the served markup: {missing}")

    page_text = " ".join(el.text for el in _walk(tree.root)).lower()
    attr_blob = " ".join(f"{k}={v}" for el in _walk(tree.root)
                         for k, v in el.attrs.items() if k.startswith("data-") or k == "class").lower()
    for state in states or []:
        s = state.strip().lower()
        if s and s not in page_text and s not in attr_blob:
            violations.append(f"declared state {state!r} is not renderable: no class, data-* value "
                              f"or text mentions it")

    served_controls: Counter = Counter()
    for el in _walk(tree.root):
        kind = _control_kind(el)
        if kind:
            served_controls[kind] += 1
    for kind, count in sorted(spec.controls.items()):
        if served_controls.get(kind, 0) < count:
            violations.append(f"control inventory short: wireframe screen has {count} x {kind}, "
                              f"served page has {served_controls.get(kind, 0)}")

    remote = _remote_urls_in_markup(tree, own_origin) + _remote_urls_in_css(css, own_origin)
    for url in sorted(set(remote)):
        violations.append(f"remote asset URL {url!r}: the wireframe is self-contained and the "
                          f"build must be too (no CDN scripts, fonts, tiles, or trackers)")
    return violations


# ------------------------------------------------------------------- check-definition generation

def emit_check_definition(project: str, screen_id: str, wireframe_file: str,
                          url_template: str, states: list[str] | None = None,
                          artifact_dir: str | None = None) -> dict:
    """The ``building-validation`` check DEFINITION for one surface binding — shaped exactly as
    :func:`agent_factory.ingestion_api.plan_time_author_check` writes it (that function is the
    single writer of the snapshot; this function only generates). Surface-bound via ``meta.surfaces``
    so RESOLVE's surface lane pins it precisely onto tickets that render this screen — never
    ``applies_to:["*"]``.

    The ``run`` chains the static parse gate (cheap, fast) with the browser-rendered gate
    (:mod:`agent_factory.wireframe_browser_check` — the decisive one, with screenshot evidence).
    """
    url = url_template.format(screen=screen_id)
    art = artifact_dir or f".af-artifacts/wireframe-conformance/{screen_id}"
    states_arg = f" --states {','.join(states)}" if states else ""
    run = (f"python -m agent_factory.wireframe_conformance check --wireframe {wireframe_file} "
           f"--screen {screen_id} --url {url}{states_arg}"
           f" && python -m agent_factory.wireframe_browser_check --wireframe {wireframe_file} "
           f"--screen {screen_id} --url {url} --artifact-dir {art}")
    return {
        "insight": (f"The served {screen_id} page conforms to the wireframe {wireframe_file}: "
                    f"styled (stylesheet floor derived from the wireframe's own CSS), carries its "
                    f"structural class and control inventory, renders its declared states, uses no "
                    f"remote assets, and matches its rendered layout in a real browser "
                    f"(screenshot evidence required)."),
        "source": f"prd-{project}",
        "category": "check",
        "scope": "validation",
        "space": project,
        "snapshot": "building-validation",
        "meta": {
            "check_id": f"wireframe-conformance-{screen_id}",
            "applies_to": [],
            "surfaces": [screen_id],
            "run": run,
        },
    }


def _load_bindings(path: str) -> list[dict]:
    """Rows from a ``praxis_list_surface_bindings`` dump: tolerate both camelCase and snake_case."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("bindings") if isinstance(data, dict) else data
    out = []
    for r in rows or []:
        meta = r.get("meta") or {}
        out.append({
            "screen_id": r.get("screen_id") or r.get("screenId") or meta.get("screen_id") or "",
            "file": r.get("file") or meta.get("file") or "",
            "states": r.get("states") or meta.get("states") or [],
        })
    return out


# ----------------------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wireframe_conformance", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    chk = sub.add_parser("check", help="audit a served page against its wireframe screen")
    chk.add_argument("--wireframe", required=True)
    chk.add_argument("--screen", required=True)
    src = chk.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="URL of the served page (fetched)")
    src.add_argument("--html-file", help="path to a served-page snapshot (offline mode)")
    chk.add_argument("--states", default="", help="comma-separated declared states")
    chk.add_argument("--style-floor-ratio", type=float, default=DEFAULT_STYLE_FLOOR_RATIO)

    em = sub.add_parser("emit", help="emit building-validation check definition(s) as JSON")
    em.add_argument("--project", required=True)
    em.add_argument("--wireframe", help="wireframe file (single-screen mode)")
    em.add_argument("--screen", help="screen id (single-screen mode)")
    em.add_argument("--states", default="")
    em.add_argument("--bindings-json", help="praxis_list_surface_bindings dump: one check per binding")
    em.add_argument("--url-template", required=True,
                    help="served URL with {screen} placeholder, e.g. http://localhost:8000/{screen}")
    em.add_argument("--artifact-dir", default=None)

    args = ap.parse_args(argv)
    if args.cmd == "check":
        try:
            wf = Path(args.wireframe).read_text(encoding="utf-8")
            spec = screen_spec(wf, args.screen)
            served = _fetch(args.url) if args.url else Path(args.html_file).read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            print(f"wireframe-conformance: cannot run: {exc}", file=sys.stderr)
            return 2
        states = [s for s in args.states.split(",") if s.strip()]
        violations = audit_served(served, spec, states=states, base_url=args.url,
                                  style_floor_ratio=args.style_floor_ratio)
        for v in violations:
            print(f"VIOLATION [{args.screen}]: {v}")
        if not violations:
            print(f"OK [{args.screen}]: served page conforms to {args.wireframe} "
                  f"({len(spec.structural_classes)} structural classes, "
                  f"{sum(spec.controls.values())} controls, style floor cleared)")
        return 1 if violations else 0

    # emit
    if args.bindings_json:
        rows = _load_bindings(args.bindings_json)
        for r in rows:  # praxis_list_surface_bindings rows carry only ids; the surface fact's
            r["file"] = r["file"] or args.wireframe or ""  # meta.file (or --wireframe) supplies it
    elif args.wireframe and args.screen:
        rows = [{"screen_id": args.screen, "file": args.wireframe,
                 "states": [s for s in args.states.split(",") if s.strip()]}]
    else:
        print("emit needs --bindings-json or --wireframe + --screen", file=sys.stderr)
        return 2
    # A binding that cannot be emitted is a LOUD error, never a silently thinner check set —
    # an unenforced surface is exactly the gap this generator exists to close.
    bad = [r for r in rows if not (r["screen_id"] and r["file"])]
    if bad:
        print(f"emit: {len(bad)} binding(s) lack a screen_id or wireframe file (pass --wireframe "
              f"or store meta.file on the surface fact): {bad}", file=sys.stderr)
        return 2
    defs = [emit_check_definition(args.project, r["screen_id"], r["file"], args.url_template,
                                  states=r["states"], artifact_dir=args.artifact_dir)
            for r in rows]
    print(json.dumps(defs, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
