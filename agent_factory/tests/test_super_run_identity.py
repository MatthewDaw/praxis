"""Pins af-super-run's project-identity resolution (ticket R1 / d4311c0603194f3285693d62b1320924):

  * an explicit ``--project`` argument is used VERBATIM (only a leading ``prd-`` stripped, matching
    the completeness endpoints' own bare-name expectation — F1, the double-prefix silent-fake-pass);
  * absent an argument, the ``FACTORY_PROJECT`` env var (else an idea-derived slug) is used, and that
    derived name is recorded as a decision episode BEFORE the caller can perform any other write;
  * a run that cannot resolve a name from any of the three sources refuses to start (returns
    ``None``) instead of inventing one mid-flight;
  * the resolved name is never ``prd-``-prefixed, so a caller handing it straight to
    ``incomplete_requirements`` / the completeness endpoints can never double-prefix it.
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _super_run_identity as identity  # noqa: E402


def test_explicit_project_arg_used_verbatim_no_episode():
    calls = []
    resolved = identity.resolve_project_identity(
        "team-app", record_episode_fn=lambda *a, **k: calls.append((a, k)))
    assert resolved == "team-app"
    assert calls == []  # explicit argument never triggers the episode write


def test_explicit_project_arg_strips_prd_prefix():
    calls = []
    resolved = identity.resolve_project_identity(
        "prd-team-app", record_episode_fn=lambda *a, **k: calls.append((a, k)))
    assert resolved == "team-app"
    assert calls == []


def test_falls_back_to_factory_project_env_and_records_episode(monkeypatch):
    monkeypatch.setenv("FACTORY_PROJECT", "prd-widgets")
    calls = []
    resolved = identity.resolve_project_identity(
        None, record_episode_fn=lambda *a, **k: calls.append((a, k)))
    assert resolved == "widgets"
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert "widgets" in args[0]
    assert kwargs["episode"]["resolved"] == "widgets"
    assert kwargs["episode"]["source"] == "env:FACTORY_PROJECT"


def test_falls_back_to_idea_slug_and_records_episode_before_return(monkeypatch):
    monkeypatch.delenv("FACTORY_PROJECT", raising=False)
    calls = []
    resolved = identity.resolve_project_identity(
        None, idea="A Rough New Idea!", record_episode_fn=lambda *a, **k: calls.append((a, k)))
    assert resolved == "a-rough-new-idea"
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["episode"]["resolved"] == "a-rough-new-idea"
    assert kwargs["episode"]["source"] == "idea-slug"


def test_refuses_when_nothing_resolves(monkeypatch):
    monkeypatch.delenv("FACTORY_PROJECT", raising=False)
    calls = []
    resolved = identity.resolve_project_identity(
        None, idea=None, record_episode_fn=lambda *a, **k: calls.append((a, k)))
    assert resolved is None
    assert calls == []  # never invents a name, never records a phantom episode


def test_project_arg_takes_priority_over_env_and_idea(monkeypatch):
    monkeypatch.setenv("FACTORY_PROJECT", "env-project")
    calls = []
    resolved = identity.resolve_project_identity(
        "arg-project", idea="some idea", record_episode_fn=lambda *a, **k: calls.append((a, k)))
    assert resolved == "arg-project"
    assert calls == []
