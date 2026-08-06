"""release() must not stamp a completion timestamp — the SERVER owns it.

``meta.finished_at`` used to have two producers that disagreed on shape: the
backend's lease-release path wrote a fixed-format UTC ISO-8601 string, and this
client wrote a bare ``time.time()`` float, so one plan carried both. The epoch rows
sort as text outside ``snapshots_finished_at_idx``'s ISO range bounds and silently
drop out of any range query — a short answer, not an error.

The fix is one producer, not two that agree: a client cannot know when a write it
has not made yet will land, so it does not guess. ``release()`` writes
``build_state`` and the server dates it (``knowledge/finished_at.py``). These tests
lock that no client write path sends a ``finished_at`` at all, so a second producer
cannot come back.

Stubs ``_praxis`` so nothing touches the network — a pure behavior lock on the patch
dicts the hook builds.
"""

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402
from _fake_praxis import SanctionedWrites  # noqa: E402


class _FakePraxis(SanctionedWrites):
    """Records patch_meta calls and lets _meta() read back the fact's current meta."""

    def __init__(self, initial_meta):
        self.meta = dict(initial_meta)
        self.patches = []

    def get_fact(self, cid, **kw):
        return {"id": cid, "meta": dict(self.meta)}

    def patch_meta(self, cid, patch, **kw):
        self.patches.append(dict(patch))
        for k, v in patch.items():
            if v is None:
                self.meta.pop(k, None)
            else:
                self.meta[k] = v
        return True


def _patched(monkeypatch, initial_meta):
    fake = _FakePraxis(initial_meta)
    monkeypatch.setattr(ts, "_praxis", fake)
    return fake


def test_release_finished_sets_build_state_and_never_finished_at(monkeypatch):
    fake = _patched(monkeypatch, {"claim_owner": "me"})

    assert ts.release("T1", "me", state="finished") is True

    assert fake.patches, "release() must call patch_meta"
    assert fake.patches[-1]["build_state"] == "finished", "the server dates THIS write"
    for sent in fake.patches:
        assert "finished_at" not in sent, (
            "the client must not stamp a completion timestamp — it does not own the "
            "clock, and a second producer is exactly the shape drift this removed"
        )


def test_release_incomplete_never_stamps_finished_at(monkeypatch):
    fake = _patched(monkeypatch, {"claim_owner": "me"})

    assert ts.release("T2", "me", state="incomplete") is True

    assert fake.meta.get("build_state") == "incomplete"
    for sent in fake.patches:
        assert "finished_at" not in sent


def test_no_client_lifecycle_write_stamps_finished_at(monkeypatch):
    """Not just release(): no lifecycle write in the hook sends a finished_at."""
    fake = _patched(monkeypatch, {})

    ts.claim("T3", "me")
    ts.heartbeat("T3", "me")
    ts.stamp_run(["T3"], "me")
    ts.record_validation_pass("T3", "v1", True)
    ts.release("T3", "me", state="finished")
    ts.block("T3", "me", "needs a credential")

    assert fake.patches
    for sent in fake.patches:
        assert "finished_at" not in sent
