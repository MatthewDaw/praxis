"""The Stop-hook gates must be INERT in a directory that was never set up for the factory.

Both gates install at USER scope, so they execute in every Claude session in every directory. They
resolved a project from the CWD BASENAME and then asked Praxis about it, so a plain session in any
unrelated repo produced a BLOCKING Stop-hook error:

    build-completeness gate: PRAXIS UNREACHABLE ...
      reason: Praxis GET /requirements/incomplete -> HTTP 403:
              {"detail":"API key is not scoped to org 'agent-factory'"}

Nothing was down. The gate had asked about a project that was never meant to exist and reported its
own bad input as an infrastructure outage — the same shape that fired twice inside this repo when a
shell was simply left in a subdirectory.

Two properties are pinned here, and the NEGATIVE ones matter most: a gate that stands down too
eagerly is worse than the noise it replaces, because the enforcement it exists to provide would
disappear silently.

  1. No factory configuration in the directory -> stand down, byte-identical to no hook, and WITHOUT
     touching the network (an unrelated directory must not even open a socket).
  2. "That space/org does not exist" (a CONFIGURATION answer) stands down, while a genuine outage
     (connection refused, 5xx, timeout) still fails CLOSED.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parent.parent / "hooks"
if str(_HOOKS) not in sys.path:
    sys.path.insert(0, str(_HOOKS))

from _gate_common import factory_configured, not_a_factory_project  # noqa: E402

GATES = ("plan_completeness_gate.py", "build_completeness_gate.py")


@pytest.mark.parametrize("gate", GATES)
def test_gate_is_byte_identical_to_no_hook_outside_a_factory_project(tmp_path, gate):
    """The regression itself: run the real hook as a subprocess from an unrelated directory."""
    env = {k: v for k, v in os.environ.items() if k != "FACTORY_PROJECT"}
    proc = subprocess.run(
        [sys.executable, str(_HOOKS / gate)],
        input=json.dumps({"cwd": str(tmp_path), "transcript_path": ""}),
        capture_output=True, text=True, cwd=str(tmp_path), env=env,
    )
    assert proc.returncode == 0
    # Zero bytes on BOTH streams: no block, and no `[praxis-hook] env=...` banner either, which
    # means the gate stood down before importing _praxis at all.
    assert proc.stdout == "", f"{gate} emitted a hook decision outside a factory project"
    assert proc.stderr == "", f"{gate} emitted noise outside a factory project"


def test_a_pinned_factory_project_still_arms(tmp_path, monkeypatch):
    """The guard must not turn the gates off everywhere: FACTORY_PROJECT means "gate this".

    ``chdir`` matters: the check looks at the PROCESS directory as well as the payload's, because
    the dotenv the gate loads moments later is found relative to the process. Running this from the
    repo (which is configured) without chdir would assert against the repo's own config, not the
    empty directory the test means to describe.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FACTORY_PROJECT", raising=False)
    assert factory_configured(str(tmp_path)) is False
    monkeypatch.setenv("FACTORY_PROJECT", "some-project")
    assert factory_configured(str(tmp_path)) is True


def test_settings_file_marks_a_directory_as_factory_configured_from_any_depth(tmp_path, monkeypatch):
    """A build worktree is recognised by its settings file, from the root or any subdirectory."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.local.json").write_text(
        json.dumps({"env": {"FACTORY_PROJECT": "proj", "PRAXIS_ORG": "praxis"}})
    )
    deep = tmp_path / "src" / "pkg"
    deep.mkdir(parents=True)
    assert factory_configured(str(tmp_path)) is True
    assert factory_configured(str(deep)) is True, "the walk up to the project root is missing"


@pytest.mark.parametrize("message", [
    "Praxis GET /facts/by -> HTTP 404: {\"detail\":\"unknown space 'somerepo'\"}",
    "Praxis GET /requirements/incomplete -> HTTP 403: {\"detail\":\"API key is not scoped to org 'x'\"}",
])
def test_a_missing_project_stands_the_gate_down(message):
    assert not_a_factory_project(Exception(message)) is True


@pytest.mark.parametrize("message", [
    "Connection refused",
    "Praxis GET /facts/by -> HTTP 500: internal error",
    "read timed out",
    "Praxis GET /facts/by -> HTTP 401: token expired",
])
def test_a_real_outage_still_fails_closed(message):
    """The whole point of the gates. Availability failures must NEVER be read as "no project here"
    -- that would convert every outage into a silent pass, which is strictly worse than blocking."""
    assert not_a_factory_project(Exception(message)) is False
