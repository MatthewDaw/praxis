"""Locks "finished implies committed" in ``release()`` (``hooks/_ticket_state.py``).

THE BUG THIS LOCKS. A fanned-out worker builds its ticket inside an ISOLATED git worktree, and the
orchestrator integrates finished tickets by MERGING each worker's branch. The §8 worker contract ran
CLAIM → READ → AUTHOR EVALS → CONFIRM RED → BUILD → CONFIRM GREEN → FINISH and never told the worker
to COMMIT, so a worker could pass every eval, call ``release(state="finished")``, and return with all
of its work sitting uncommitted. There was nothing on its branch to merge, so the orchestrator
improvised: it swept the stray files into catch-all ``wip: salvage uncommitted worker output``
commits on the run branch. That launders unverified edits — possibly from a worker that died between
CONFIRM RED and CONFIRM GREEN — into the branch under cover of a green run, which is exactly the
silent-partial-failure class the gates exist to prevent. One real run landed several such commits.

The guard is deliberately POSITIVE-EVIDENCE ONLY: it blocks on a definite "dirty" answer and never on
"cannot tell" (git missing, not a repo), so a worker legitimately running outside a worktree can still
finish.
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402
from _fake_praxis import SanctionedWrites  # noqa: E402

PLAN = ("team-app", "prd-team-app")


class FakePraxis(SanctionedWrites):
    def __init__(self, meta=None):
        self._meta = dict(meta or {})
        self.writes = []

    def get_fact(self, cid, *, space=None, snapshot=None):
        return {"id": cid, "meta": dict(self._meta)}

    def patch_meta(self, cid, meta_dict, *, space=None, snapshot=None):
        self.writes.append(dict(meta_dict))
        self._meta.update(meta_dict)
        return {"id": cid, "meta": dict(self._meta)}


def _claimed(owner="agent-A"):
    return {ts.M_BUILD_STATE: "in_progress", ts.M_CLAIM_OWNER: owner,
            ts.M_CLAIM_AT: 123.0, ts.M_CLAIM_HEARTBEAT_AT: 123.0,
            ts.M_CLAIM_LEASE_TTL: 900, "requirement_id": "R7"}


def _enforce(monkeypatch):
    """Opt back INTO the guard (conftest disables it for the rest of the unit suite)."""
    monkeypatch.delenv("AF_ALLOW_DIRTY_FINISH", raising=False)


def test_finish_is_refused_while_the_worktree_is_dirty(monkeypatch, capsys):
    """The regression: finishing with uncommitted work must NOT write a finished state."""
    _enforce(monkeypatch)
    fake = FakePraxis(_claimed())
    monkeypatch.setattr(ts, "_praxis", fake)
    monkeypatch.setattr(ts, "uncommitted_changes", lambda cwd=None: " M knowledge/serve/app.py")

    assert ts.release("R7", "agent-A", "finished", ref=PLAN) is False
    assert not fake.writes, "a refused finish must write NOTHING"
    assert fake.get_fact("R7")["meta"][ts.M_BUILD_STATE] == "in_progress"

    err = capsys.readouterr().err  # and it is LOUD, naming the ticket and the offending files
    assert "REFUSING to finish" in err and "R7" in err
    assert "knowledge/serve/app.py" in err


def test_finish_succeeds_on_a_clean_worktree(monkeypatch):
    _enforce(monkeypatch)
    fake = FakePraxis(_claimed())
    monkeypatch.setattr(ts, "_praxis", fake)
    monkeypatch.setattr(ts, "uncommitted_changes", lambda cwd=None: "")

    assert ts.release("R7", "agent-A", "finished", ref=PLAN) is True
    assert fake.get_fact("R7")["meta"][ts.M_BUILD_STATE] == "finished"


def test_yielding_incomplete_is_never_blocked_by_a_dirty_tree(monkeypatch):
    """A worker yielding mid-build is EXPECTED to have uncommitted work — only ``finished`` implies
    committed. Blocking the yield would strand the lease on a ticket nobody is building."""
    _enforce(monkeypatch)
    fake = FakePraxis(_claimed())
    monkeypatch.setattr(ts, "_praxis", fake)
    monkeypatch.setattr(ts, "uncommitted_changes", lambda cwd=None: " M half/done.py")

    assert ts.release("R7", "agent-A", "incomplete", ref=PLAN) is True
    assert fake.get_fact("R7")["meta"][ts.M_BUILD_STATE] == "incomplete"


def test_cannot_tell_does_not_block_the_finish(monkeypatch):
    """Positive evidence only: outside a git repo the guard must stay out of the way."""
    _enforce(monkeypatch)
    fake = FakePraxis(_claimed())
    monkeypatch.setattr(ts, "_praxis", fake)
    monkeypatch.setattr(ts, "uncommitted_changes", lambda cwd=None: "")

    assert ts.release("R7", "agent-A", "finished", ref=PLAN) is True


def test_explicit_override_allows_a_dirty_finish(monkeypatch):
    """The escape hatch is deliberate and must be explicit — never the default."""
    monkeypatch.setenv("AF_ALLOW_DIRTY_FINISH", "1")
    fake = FakePraxis(_claimed())
    monkeypatch.setattr(ts, "_praxis", fake)
    monkeypatch.setattr(ts, "uncommitted_changes",
                        lambda cwd=None: (_ for _ in ()).throw(AssertionError("must not be consulted")))

    assert ts.release("R7", "agent-A", "finished", ref=PLAN) is True


def test_uncommitted_changes_reports_clean_when_git_cannot_answer(tmp_path):
    """The real helper (not a stub) must return "" — never raise — outside a git repo."""
    assert ts.uncommitted_changes(cwd=str(tmp_path)) == ""
