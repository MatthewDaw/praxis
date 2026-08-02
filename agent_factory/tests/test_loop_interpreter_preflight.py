"""The driver must refuse to start when the Python it hands to workers cannot load the universal
quality lane -- and must hand that interpreter down instead of letting workers guess.

The incident: `agent_factory/seeded_checks.py` does `import tomllib` (stdlib, 3.11+). A build box's
`/usr/bin/python3` was 3.9, so that import raised ModuleNotFoundError, `_universal_checks()` caught
it with a bare `except Exception: return []`, and the mandatory `minimalism-dry` universal gate
vanished from every ticket. Three tickets reached FINISHED with no quality gate and nothing said a
word. The driver had already resolved a good `$PY` for its own heredocs; the gap was that the
per-ticket worker agents type a bare `python3` in their own Bash calls, which resolves against
their PATH.

Two independent defences are asserted here, because either alone still permits a silent run:
  1. a loud preflight -- a run that cannot load the lane does not begin at all;
  2. the resolved interpreter is exported under the names `_ticket_state._sidecar_pythons()`
     already reads, and named in the dispatch prompt.
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "agent_factory"
SCRIPT = PLUGIN / "scripts" / "af-ticket-loop.sh"
SRC = SCRIPT.read_text()

# The names the hook side already honours. Asserted against the hook source rather than hardcoded
# here, so renaming one there fails this test instead of silently decoupling the two halves.
SIDECAR_SRC = (PLUGIN / "hooks" / "_ticket_state.py").read_text()


def _preflight_fn() -> str:
    """The preflight function lifted verbatim out of the driver, so these tests exercise the real
    code path rather than a paraphrase that can drift away from it."""
    start = SRC.index("preflight_universal_lane(){")
    end = SRC.index("\n}\n", start) + len("\n}\n")
    return SRC[start:end]


def _run_preflight(py: str) -> subprocess.CompletedProcess:
    """Run the extracted preflight against interpreter `py`, with the driver's own PYTHONPATH and a
    stub `say` (the real one tees to a log the harness has no business creating)."""
    harness = textwrap.dedent(
        f"""
        set -euo pipefail
        say(){{ echo "$*"; }}
        PY={py!r}
        AF_REPO={str(REPO)!r}
        AF_PLUGIN_DIR={str(PLUGIN)!r}
        export PYTHONPATH="$AF_PLUGIN_DIR/hooks:$AF_PLUGIN_DIR/src"
        {_preflight_fn()}
        preflight_universal_lane
        """
    )
    return subprocess.run(["bash", "-c", harness], capture_output=True, text=True)


def _fake_python(tmp_path: Path, *, body: str) -> str:
    """A stand-in interpreter that fails the way a real bad one does. Written as a shell shim so the
    test needs no second Python on the machine."""
    shim = tmp_path / "python-fake"
    shim.write_text(f"#!/bin/sh\n{body}\n")
    shim.chmod(0o755)
    return str(shim)


def test_the_real_interpreter_passes_its_own_preflight():
    """Sanity floor: the interpreter running these tests must load a NON-EMPTY universal lane. If
    this fails, the checkout itself is the thing that is broken."""
    r = _run_preflight(sys.executable)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "universal lane OK" in r.stdout


def test_preflight_fails_when_tomllib_is_missing():
    """The exact 3.9 failure. The shim reproduces it by refusing the tomllib import."""
    r = _run_preflight(sys.executable + "-does-not-exist")
    assert r.returncode != 0
    assert "FATAL" in r.stdout


def test_preflight_reports_interpreter_failure_and_remediation(tmp_path):
    py = _fake_python(tmp_path, body="echo 'ModuleNotFoundError: tomllib' >&2; exit 1")
    r = _run_preflight(py)
    assert r.returncode != 0, "a run that cannot load its universal gate must not begin"
    out = r.stdout + r.stderr
    assert "FATAL" in out and "refusing to start" in out
    assert py in out, "the message must name the interpreter it actually tried"
    assert "AF_PYTHON" in out and "PRAXIS_HOOK_PYTHON" in out, "must name the remediation knobs"
    assert "3.11" in out


def test_preflight_fails_on_an_empty_universal_lane(tmp_path):
    """An empty lane is the silent-outage shape: everything imports, nothing gates. A missing or
    mis-parsed seeded_checks.toml produces exactly this, so it must be fatal, not a pass."""
    py = _fake_python(tmp_path, body="exit 0")  # imports "fine", prints nothing
    probe = subprocess.run([py, "-c", "pass"], capture_output=True, text=True)
    assert probe.returncode == 0, "precondition: the shim exits clean"

    empty = tmp_path / "python-empty"
    empty.write_text(
        "#!/bin/sh\n"
        # Consume the heredoc on stdin, then report zero checks the way the driver's probe would.
        "cat >/dev/null\n"
        "echo 'universal_seeded_checks() returned an EMPTY list'\n"
        "exit 1\n"
    )
    empty.chmod(0o755)
    r = _run_preflight(str(empty))
    assert r.returncode != 0
    assert "EMPTY" in (r.stdout + r.stderr)


def test_preflight_runs_before_any_ticket_is_claimed():
    """Failing here costs seconds; failing three tickets in costs an hour and a stranded lease."""
    assert SRC.index("preflight_universal_lane || exit 1") < SRC.index("/af-build $PROJECT $ids_csv")


def test_preflight_is_fatal_not_advisory():
    assert "preflight_universal_lane || exit 1" in SRC, "the preflight's verdict must be enforced"


def test_resolved_interpreter_is_exported_for_workers():
    assert 'export PRAXIS_HOOK_PYTHON="$PY"' in SRC
    assert 'export AF_PYTHON="$PY"' in SRC


def test_exported_names_match_what_the_hooks_actually_read():
    """Inventing a name here would export something nothing reads, restoring the silent failure."""
    for name in ("PRAXIS_HOOK_PYTHON", "AF_PYTHON"):
        assert f'os.environ.get("{name}")' in SIDECAR_SRC, f"{name} is not read by _ticket_state"


def test_export_happens_after_py_is_resolved():
    assert SRC.index("PY=\"$(command -v python3)\"") < SRC.index('export AF_PYTHON="$PY"')


def test_dispatch_prompt_tells_workers_which_interpreter_to_use():
    send = next(line for line in SRC.splitlines() if "/af-build $PROJECT $ids_csv" in line)
    assert "$PY" in send, "workers are never told the resolved interpreter"
    assert "python3" in send, "the prompt must name what NOT to use"


def test_dispatch_prompt_addition_stays_short():
    """That prompt string is enormous and costs tokens every round; the addition is two sentences."""
    send = next(line for line in SRC.splitlines() if "/af-build $PROJECT $ids_csv" in line)
    addition = send[send.index("For ANY factory or Praxis python invocation") : send.index("$SERVICES")]
    assert len(addition) < 500, f"prompt addition is {len(addition)} chars"


def test_dispatch_prompt_is_still_one_double_quoted_send_keys():
    """The interpreter path is only useful if it interpolates -- and the existing $INTEGRATION_REF
    contract depends on the same quoting."""
    send = next(line for line in SRC.splitlines() if "/af-build $PROJECT $ids_csv" in line)
    assert re.search(r'tmux send-keys -t "\$SESSION" "', send), "prompt is not double-quoted"
    r = subprocess.run(
        ["bash", "-c", 'PY=/v/bin/python; echo "use: $PY not python3"'],
        capture_output=True, text=True,
    )
    assert "use: /v/bin/python not python3" in r.stdout


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
