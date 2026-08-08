"""The seam that lets ``agent_factory.*`` import ``hooks/*`` however the process was launched.

These tests exist because the in-process suite CANNOT see the bug they guard. ``pytest``'s
``pythonpath = ["src", ".", "hooks"]`` puts ``agent_factory/`` itself on ``sys.path``, so
``from hooks import _praxis`` resolves inside a test no matter how broken the production seam is.
The loop launches with ``PYTHONPATH=<root>/hooks:<root>/src`` and cwd at the *repository* root --
neither of which contains ``hooks/``'s parent -- and that is where it died. So the load-bearing
test here shells out to a subprocess configured exactly like the loop, and the in-process tests
only cover the properties a subprocess cannot cheaply observe (module identity, packaging).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

# tests/ -> agent_factory/ -> repository root
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent

# The four modules whose top-level imports reach into hooks/. All of them must survive a loop-style
# launch; ingestion_api is the one af-retro (the loop's actual call at af-ticket-loop.sh:2485)
# pulls in transitively.
HOOK_DEPENDENT_MODULES = (
    "agent_factory.ingestion_api",
    "agent_factory.af_learn",
    "agent_factory.resolution",
    "agent_factory.af_retro",
)


def _run_like_the_loop(code: str) -> subprocess.CompletedProcess:
    """Run ``code`` with the loop's PYTHONPATH and cwd, and nothing else from this process.

    The env is rebuilt rather than copied for PYTHONPATH so an ambient value (pytest's, a shell's)
    cannot smuggle ``agent_factory/`` onto the path and make a broken seam look healthy.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(PACKAGE_ROOT / "hooks"), str(PACKAGE_ROOT / "src")]
    )
    return subprocess.run(
        # -S: no site-packages. Without it a developer venv with `agent-factory` pip-installed
        # supplies `hooks` from site-packages (it ships there now, by design) and the subprocess
        # succeeds no matter how broken the seam is -- the exact masking this suite exists to
        # avoid. -S leaves only stdlib + the two PYTHONPATH entries, so `hooks` can ONLY come
        # from _hooks.py doing its job.
        [sys.executable, "-S", "-c", code],
        cwd=REPO_ROOT,  # NOT PACKAGE_ROOT: cwd on sys.path would resolve `hooks` for free
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_hook_dependent_modules_import_under_the_loops_pythonpath():
    imports = "\n".join(f"import {mod}" for mod in HOOK_DEPENDENT_MODULES)
    proc = _run_like_the_loop(
        f"{imports}\n"
        "from agent_factory import ingestion_api\n"
        "print('RESOLVED', ingestion_api._praxis.__file__, ingestion_api._praxis.__name__)"
    )
    assert proc.returncode == 0, (
        "a loop-style launch cannot import the hook-dependent modules:\n" + proc.stderr
    )
    resolved = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESOLVED ")]
    assert resolved, proc.stdout + proc.stderr
    _, path, name = resolved[-1].split(" ")
    # It must be THIS checkout's hooks/, under the canonical name -- not some other copy that
    # happened to be earlier on the path, and not a bare top-level `_praxis`.
    assert Path(path).resolve() == (PACKAGE_ROOT / "hooks" / "_praxis.py").resolve()
    assert name == "hooks._praxis"


def test_loop_style_launch_does_not_put_the_repo_root_on_sys_path_for_free():
    """Guard the guard: prove the harness above is actually reproducing the hostile condition.

    If this ever fails, ``test_hook_dependent_modules_import_under_the_loops_pythonpath`` has
    stopped testing anything -- ``hooks`` would be importable without the seam.
    """
    proc = _run_like_the_loop(
        "import importlib.util\n"
        "print('BEFORE', importlib.util.find_spec('hooks') is None)\n"
        "import agent_factory._hooks  # the seam\n"
        "print('AFTER', importlib.util.find_spec('hooks') is not None)"
    )
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith(("BEFORE", "AFTER"))]
    assert lines == ["BEFORE True", "AFTER True"], (
        "expected `hooks` to be unresolvable until agent_factory._hooks runs; got "
        f"{lines!r}. If BEFORE is False the loop-style test is vacuous -- something else is "
        "already supplying `hooks` and a broken seam would go unnoticed.\n" + proc.stdout
    )


def test_hooks_resolve_to_one_module_object_under_the_canonical_name():
    """Two module objects for one file would silently break every ``monkeypatch`` on ``_praxis``."""
    import hooks._praxis
    import hooks._ticket_state

    from agent_factory import _hooks, ingestion_api

    assert _hooks._praxis is hooks._praxis
    assert _hooks._ticket_state is hooks._ticket_state
    assert ingestion_api._praxis is hooks._praxis
    assert ingestion_api._ts is hooks._ticket_state
    # Not just the objects the consumers hold: BOTH sys.modules entries must be that same object,
    # and it must be THIS checkout's file. A "fix" that put hooks/ on sys.path and imported the bare
    # top-level module would satisfy the attribute identities above and still fork the moment
    # anything else imported it under the dotted name.
    #
    # Deliberately NOT asserting ``__name__ == "hooks._praxis"``: `_canonical_module` claims only the
    # FREE sys.modules name (setdefault, never steal), so whichever name an importer reached first
    # wins and __name__ is legitimately order-dependent -- asserting it made this test pass in a full
    # run and fail in a subset that imported bare-first. Identity + file is the property that matters
    # and it holds in every order.
    for bare, mod in (("_praxis", hooks._praxis), ("_ticket_state", hooks._ticket_state)):
        assert sys.modules[bare] is mod is sys.modules[f"hooks.{bare}"], (
            f"{bare} is forked across its two import names: "
            f"{sys.modules.get(bare)!r} vs {sys.modules.get(f'hooks.{bare}')!r}"
        )
        assert Path(mod.__file__).resolve() == (PACKAGE_ROOT / "hooks" / f"{bare}.py").resolve()


_IDENTITY_PROBE = """
import sys
from agent_factory import ingestion_api          # the library route (from hooks import ...)
import hooks._praxis, hooks._ticket_state        # the dotted route
import _praxis, _ticket_state                    # the BARE route a hook subprocess uses
print("PRAXIS", _praxis is hooks._praxis is ingestion_api._praxis
      is hooks._ticket_state._praxis is sys.modules["hooks._praxis"] is sys.modules["_praxis"])
print("TICKET_STATE", _ticket_state is hooks._ticket_state is ingestion_api._ts)
hooks._praxis.MARKER = object()                  # what a monkeypatch does, on ONE of the names
print("PATCH_VISIBLE", getattr(ingestion_api._praxis, "MARKER", None)
      is getattr(_praxis, "MARKER", None) is not None)
"""


def _probe_identity(cwd, pythonpath) -> dict[str, bool]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(str(p) for p in pythonpath)
    proc = subprocess.run([sys.executable, "-c", _IDENTITY_PROBE], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return {ln.split()[0]: ln.split()[1] == "True"
            for ln in proc.stdout.splitlines() if ln.startswith(("PRAXIS", "TICKET_STATE", "PATCH_VISIBLE"))}


def test_one_module_object_per_hook_file_however_it_is_imported(tmp_path):
    """The in-process identity test above CANNOT see this: pytest's own conftest imports
    ``hooks._praxis`` before anything else, so the dotted name is already populated and the bare
    ``import _praxis`` a hook subprocess performs never happens inside the suite.

    Run from a neutral cwd with only ``src`` on the path -- the configuration a verifier used to
    prove the seam forked -- and require that BOTH names resolve to ONE object per file, and that a
    patch applied through one name is visible through the other. Before the fix this printed
    ``PRAXIS False``: ``hooks/_praxis.py`` executed twice, so every ``monkeypatch.setattr(_praxis,
    ...)`` on one object left the other untouched.
    """
    got = _probe_identity(tmp_path, [PACKAGE_ROOT / "src"])
    assert got == {"PRAXIS": True, "TICKET_STATE": True, "PATCH_VISIBLE": True}


def test_identity_holds_when_the_bare_hook_path_is_on_pythonpath_too(tmp_path):
    """The loop's own launch (``PYTHONPATH=<root>/hooks:<root>/src``) makes BOTH import routes
    resolvable at once -- the condition under which a fork is easiest to create."""
    got = _probe_identity(tmp_path, [PACKAGE_ROOT / "hooks", PACKAGE_ROOT / "src"])
    assert got == {"PRAXIS": True, "TICKET_STATE": True, "PATCH_VISIBLE": True}


def test_wheel_ships_the_hooks_package():
    """An installed wheel that omits hooks/ can never import ingestion_api.

    Config-level assertion on purpose: building and installing a wheel per test run is too slow
    for the default suite. The end-to-end proof is
    ``uv build --wheel && uv pip install <whl> && python -c 'import agent_factory.ingestion_api'``
    in an env with no PYTHONPATH.
    """
    with (PACKAGE_ROOT / "pyproject.toml").open("rb") as fh:
        pyproject = tomllib.load(fh)
    packages = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert "hooks" in packages, (
        "hooks/ is excluded from the wheel; agent_factory.ingestion_api would be unimportable "
        f"from any install (packages={packages})"
    )
    assert "src/agent_factory" in packages
