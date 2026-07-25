"""Locks the release() finished_at contract (ticket a34d3f2293ec451cb553cdddb3b27800):

A ticket whose build_state is set to "finished" through release() must carry a
``finished_at`` timestamp (epoch seconds) in its meta, stamped within one second of the
release() call. A ticket that is released as "incomplete" (or never released) must NOT
carry a finished_at — leaving it, or a stale one from a prior finish, would let a ticket
read back as done work it never actually completed this cycle.

These tests stub ``_praxis.get_fact``/``_praxis.patch_meta`` so they never touch the
network — pure behavior lock on the patch dict release() builds.
"""

import sys
import time
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _ticket_state as ts  # noqa: E402


class _FakePraxis:
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


def test_release_finished_stamps_finished_at_within_one_second(monkeypatch):
    fake = _patched(monkeypatch, {"claim_owner": "me"})
    before = time.time()
    ok = ts.release("T1", "me", state="finished")
    after = time.time()

    assert ok is True
    assert fake.patches, "release() must call patch_meta"
    last_patch = fake.patches[-1]
    assert "finished_at" in last_patch, "finished build_state must stamp finished_at"
    stamped = last_patch["finished_at"]
    assert isinstance(stamped, (int, float))
    assert before - 1 <= stamped <= after + 1

    # And it reads back on the (fake) ticket's meta.
    assert fake.meta.get("build_state") == "finished"
    assert "finished_at" in fake.meta


def test_release_incomplete_never_stamps_finished_at(monkeypatch):
    fake = _patched(monkeypatch, {"claim_owner": "me"})
    ok = ts.release("T2", "me", state="incomplete")

    assert ok is True
    assert fake.meta.get("build_state") == "incomplete"
    assert "finished_at" not in fake.meta
    for patch in fake.patches:
        assert patch.get("finished_at") is None or "finished_at" not in patch


def test_ticket_never_finished_carries_no_finished_at(monkeypatch):
    # A ticket that is merely claimed/in-progress (never released as finished) must not
    # carry a finished_at at all.
    fake = _patched(monkeypatch, {"claim_owner": "me", "build_state": "in_progress"})
    assert "finished_at" not in fake.meta
