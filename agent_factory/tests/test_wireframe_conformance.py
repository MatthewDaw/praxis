"""The mechanical wireframe-conformance gate: assertions DERIVED from the wireframe, never
hand-listed — and the literal farming_analysis stub (an f-string of title+h1+body, zero CSS) must
fail it on every axis while a faithful page passes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_factory.wireframe_conformance import (
    DEFAULT_STYLE_FLOOR_RATIO,
    audit_served,
    emit_check_definition,
    main,
    normalized_css_bytes,
    screen_spec,
)

# A miniature of the real operator wireframe: shared shell (nav + header), two screens, a script
# that toggles runtime classes, ~real CSS.
WIREFRAME = """<!doctype html><html><head><style>
/* shell */
body { margin:0; font:14px/1.5 ui-sans-serif, system-ui, sans-serif; }
.app { display:grid; grid-template-columns:216px 1fr; min-height:100vh; }
nav { border-right:1px solid #ccc; padding:18px; }
.brand { font-weight:600; }
.card { background:#fff; border:1px solid #ccc; border-radius:9px; padding:16px; }
.pill { font-size:11px; border-radius:20px; border:1px solid #ccc; }
.toggle { display:flex; gap:8px; }
.listonly { border-style:dashed; }
.hidden { display:none !important; }
button.on { font-weight:600; }
</style></head><body>
<div class="app">
  <nav>
    <div class="brand">Console</div>
    <button data-go="s-map" class="on">Map</button>
    <button data-go="s-list">List</button>
  </nav>
  <div>
    <header><h1>Map</h1><select id="site"><option>farm-a</option></select></header>
    <main>
      <section id="s-map">
        <div class="card"><span class="pill">ok</span>
          <label class="toggle"><input type="checkbox"> anomalies</label>
          <label class="toggle"><input type="checkbox"> zones</label>
          <button>Export</button>
        </div>
      </section>
      <section id="s-list" class="hidden">
        <div class="card listonly"><button>Refresh</button><button>Sort</button>
          <a href="detail.html">detail</a></div>
      </section>
    </main>
  </div>
</div>
<script>
  for (const b of document.querySelectorAll('[data-go]')) b.onclick = () => {
    document.querySelector('#' + b.dataset.go).classList.remove('hidden');
    b.classList.add('on');
  };
</script>
</body></html>"""

# The EXACT shape that shipped in farming_analysis and went green on all nine checks.
STUB = ("<html><head><title>Map view</title></head><body><h1>Map view</h1>"
        "<p>anomalies zones export</p></body></html>")

# A faithful build of s-map: full stylesheet, shell, structural classes, controls, no remote assets.
FAITHFUL = """<!doctype html><html><head><style>
body { margin:0; font:14px/1.5 ui-sans-serif, system-ui, sans-serif; }
.app { display:grid; grid-template-columns:216px 1fr; min-height:100vh; }
nav { border-right:1px solid #ccc; padding:18px; }
.brand { font-weight:600; }
.card { background:#fff; border:1px solid #ccc; border-radius:9px; padding:16px; }
.pill { font-size:11px; border-radius:20px; border:1px solid #ccc; }
.toggle { display:flex; gap:8px; }
button.primary { background:#2f5d3a; color:#fff; }
</style></head><body>
<div class="app">
  <nav><div class="brand">Console</div><button class="on">Map</button><button>List</button></nav>
  <div>
    <header><h1>Map</h1><select><option>farm-a</option></select></header>
    <main data-state="loading">
      <div class="card"><span class="pill">ok</span>
        <label class="toggle"><input type="checkbox"> anomalies</label>
        <label class="toggle"><input type="checkbox"> zones</label>
        <button>Export</button></div>
    </main>
  </div>
</div></body></html>"""


# ------------------------------------------------------------------------ spec derivation

def test_spec_is_computed_from_the_wireframe_not_hardcoded():
    spec = screen_spec(WIREFRAME, "s-map")
    # structural = used in (screen ∪ shell) AND styled by the CSS…
    assert {"app", "brand", "card", "pill", "toggle"} <= spec.structural_classes
    # …minus classes only the sibling screen uses, and minus script-toggled runtime state.
    assert "listonly" not in spec.structural_classes
    assert "hidden" not in spec.structural_classes and "on" not in spec.structural_classes
    # control inventory is per-kind counts across screen + shell.
    assert spec.controls["button"] == 3          # 2 nav + 1 Export; the sibling screen's excluded
    assert spec.controls["select"] == 1
    assert spec.controls["input[checkbox]"] == 2
    assert "a" not in spec.controls              # the detail link lives in s-list only
    assert spec.style_bytes == normalized_css_bytes(
        WIREFRAME.split("<style>")[1].split("</style>")[0])


def test_sibling_screen_scopes_differ():
    assert "listonly" in screen_spec(WIREFRAME, "s-list").structural_classes
    assert screen_spec(WIREFRAME, "s-list").controls["a"] == 1


def test_unknown_screen_raises():
    with pytest.raises(ValueError, match="s-nope"):
        screen_spec(WIREFRAME, "s-nope")


# ------------------------------------------------------------------------ the audit

def test_the_shipped_stub_fails_every_axis():
    spec = screen_spec(WIREFRAME, "s-map")
    violations = audit_served(STUB, spec, states=["loading"])
    text = "\n".join(violations)
    assert "stylesheet too small" in text
    assert "structural classes" in text
    assert "control inventory short" in text
    assert "'loading'" in text


def test_a_faithful_page_passes():
    spec = screen_spec(WIREFRAME, "s-map")
    assert audit_served(FAITHFUL, spec, states=["loading"]) == []


def test_style_floor_derives_from_the_wireframe():
    spec = screen_spec(WIREFRAME, "s-map")
    floor = int(spec.style_bytes * DEFAULT_STYLE_FLOOR_RATIO)
    thin = FAITHFUL.replace(FAITHFUL.split("<style>")[1].split("</style>")[0],
                            "body{margin:0}")
    [style_violation] = [v for v in audit_served(thin, spec) if "stylesheet too small" in v]
    assert str(floor) in style_violation


def test_remote_assets_are_rejected():
    spec = screen_spec(WIREFRAME, "s-map")
    cdn = FAITHFUL.replace("</head>",
                           '<script src="https://cdn.example.com/leaflet.js"></script>'
                           '<link rel="stylesheet" href="https://fonts.example.com/x.css"></head>')
    remote = [v for v in audit_served(cdn, spec) if "remote asset" in v]
    assert len(remote) == 2
    # …but same-origin absolute URLs are the page's own and pass.
    own = FAITHFUL.replace("</head>",
                           '<script src="http://localhost:8000/app.js"></script></head>')
    assert not [v for v in audit_served(own, spec, base_url="http://localhost:8000/map")
                if "remote asset" in v]


def test_css_url_imports_count_as_remote():
    spec = screen_spec(WIREFRAME, "s-map")
    page = FAITHFUL.replace("button.primary",
                            "@import 'https://fonts.googleapis.com/css2?family=X';\nbutton.primary")
    assert [v for v in audit_served(page, spec) if "remote asset" in v]


# ------------------------------------------------------------------------ CLI + emit

def test_cli_check_offline_mode(tmp_path):
    wf = tmp_path / "wire.html"
    wf.write_text(WIREFRAME, encoding="utf-8")
    good = tmp_path / "good.html"
    good.write_text(FAITHFUL, encoding="utf-8")
    stub = tmp_path / "stub.html"
    stub.write_text(STUB, encoding="utf-8")

    assert main(["check", "--wireframe", str(wf), "--screen", "s-map",
                 "--html-file", str(good)]) == 0
    assert main(["check", "--wireframe", str(wf), "--screen", "s-map",
                 "--html-file", str(stub)]) == 1
    assert main(["check", "--wireframe", str(wf), "--screen", "s-map",
                 "--html-file", str(tmp_path / "missing.html")]) == 2


def test_emit_generates_surface_bound_definition_chaining_both_gates():
    d = emit_check_definition("farm", "s-map", "docs/wireframes/operator.html",
                              "http://localhost:8000/{screen}", states=["loading", "empty"])
    assert d["category"] == "check" and d["scope"] == "validation"
    assert d["space"] == "farm" and d["snapshot"] == "building-validation"
    assert d["meta"]["check_id"] == "wireframe-conformance-s-map"
    assert d["meta"]["surfaces"] == ["s-map"]        # surface-bound, never ["*"]
    assert d["meta"]["applies_to"] == []
    run = d["meta"]["run"]
    assert "wireframe_conformance check" in run and "wireframe_browser_check" in run
    assert "--url http://localhost:8000/s-map" in run
    assert "--states loading,empty" in run
    assert "--artifact-dir" in run


def test_emit_from_bindings_dump(tmp_path, capsys):
    dump = tmp_path / "bindings.json"
    dump.write_text(json.dumps({"bindings": [
        {"screenId": "s-map", "meta": {"file": "docs/wireframes/operator.html",
                                       "states": ["loading"]}},
        {"screen_id": "s-list", "file": "docs/wireframes/operator.html", "states": []},
    ]}), encoding="utf-8")
    assert main(["emit", "--project", "farm", "--bindings-json", str(dump),
                 "--url-template", "http://localhost:8000/{screen}"]) == 0
    defs = json.loads(capsys.readouterr().out)
    assert [d["meta"]["check_id"] for d in defs] == ["wireframe-conformance-s-map",
                                                     "wireframe-conformance-s-list"]


def test_emit_is_loud_when_a_binding_cannot_be_enforced(tmp_path, capsys):
    """A praxis_list_surface_bindings row carries only ids; without meta.file or --wireframe the
    emit REFUSES (exit 2) rather than silently emitting a thinner check set — an unenforced
    surface is the gap this generator closes."""
    dump = tmp_path / "bindings.json"
    dump.write_text(json.dumps({"bindings": [{"screenId": "s-map"}]}), encoding="utf-8")
    assert main(["emit", "--project", "farm", "--bindings-json", str(dump),
                 "--url-template", "http://localhost:8000/{screen}"]) == 2
    assert "lack a screen_id or wireframe file" in capsys.readouterr().err
    # …and --wireframe as the fleet-wide default file makes the same dump emittable.
    assert main(["emit", "--project", "farm", "--bindings-json", str(dump),
                 "--wireframe", "docs/wireframes/operator.html",
                 "--url-template", "http://localhost:8000/{screen}"]) == 0


def test_module_is_runnable_as_a_check_command():
    """The emitted run command shells `python -m agent_factory.wireframe_conformance` — prove the
    module entrypoint exists (non-zero-but-controlled exit on missing args, not ImportError)."""
    proc = subprocess.run([sys.executable, "-m", "agent_factory.wireframe_conformance"],
                          capture_output=True, text=True,
                          cwd=str(Path(__file__).resolve().parents[1] / "src"))
    assert "ImportError" not in proc.stderr and "ModuleNotFoundError" not in proc.stderr
    assert proc.returncode == 2  # argparse usage error
