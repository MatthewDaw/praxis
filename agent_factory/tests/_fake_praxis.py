"""Give a ``_praxis`` test double the SANCTIONED build-state routes.

``_ticket_state`` no longer writes a ticket's build state through ``patch_meta``
(``PATCH /candidates/{cid}``): a blessed ``prd-<project>`` plan refuses candidate edits
(the S12 bless guard), so on a blessed plan a claim, a check pin and a finish were all
refused and the build loop could not run at all. Build state is not plan content, so it
now goes through ``/requirements/{cid}/claim``, ``/requirements/{cid}/release`` and
``/requirements/{cid}/build-state``.

A double that implements only ``get_fact``/``patch_meta`` therefore stops seeing those
writes. Mixing :class:`SanctionedWrites` in re-expresses the three routes in terms of the
double's OWN ``get_fact``/``patch_meta``, so every existing assertion over recorded writes
keeps working and keeps meaning what it meant.

WHAT IT DELIBERATELY DOES NOT EMULATE. This is a stand-in for the transport, not a second
implementation of the server. The build-state key ALLOWLIST, the ``finished_at`` stamp and
the "refuse to finish a ticket nothing gates" guard all live in
``PostgresVectorGraph`` and are pinned by their own tests against the real code
(``knowledge/knowledge_graph/tests/``). Re-implementing them here would only prove this
file agrees with itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, _HOOKS)

import _ticket_state as _ts  # noqa: E402


class SanctionedWrites:
    """Mixin: the sanctioned build-state routes, backed by the double's own storage."""

    def write_build_state(self, cid, meta_dict, *, owner=None, space=None, snapshot=None):
        """``POST /requirements/{cid}/build-state`` — a plain meta merge here.

        The server's key allowlist is not re-checked (see the module docstring); the
        optional lease check is, because callers rely on its return value."""
        if owner is not None:
            held = (self.get_fact(cid, space=space, snapshot=snapshot).get("meta") or {}).get(
                _ts.M_CLAIM_OWNER
            )
            if held is not None and held != owner:
                return None
        return self.patch_meta(cid, dict(meta_dict), space=space, snapshot=snapshot)

    def claim_requirement(self, cid, owner, ttl, *, space=None, snapshot=None):
        """``POST /requirements/{cid}/claim`` — grant iff not held by a different LIVE lease.

        Returns ``None`` on conflict, mirroring ``_praxis.claim_requirement``'s "409 is a
        normal answer, not an outage" contract."""
        meta = self.get_fact(cid, space=space, snapshot=snapshot).get("meta") or {}
        if _ts._lease_live(meta) and meta.get(_ts.M_CLAIM_OWNER) != owner:
            return None
        now = _ts.time.time()
        patch = {
            _ts.M_BUILD_STATE: "in_progress",
            _ts.M_CLAIM_OWNER: owner,
            _ts.M_CLAIM_AT: meta.get(_ts.M_CLAIM_AT)
            if meta.get(_ts.M_CLAIM_OWNER) == owner and meta.get(_ts.M_CLAIM_AT) is not None
            else now,
            _ts.M_CLAIM_HEARTBEAT_AT: now,
            _ts.M_CLAIM_LEASE_TTL: int(ttl),
        }
        self.patch_meta(cid, patch, space=space, snapshot=snapshot)
        return dict(patch)

    def release_requirement(self, cid, owner, state, *, honor_takeover=False,
                            space=None, snapshot=None):
        """``POST /requirements/{cid}/release`` — drop the lease and stamp a terminal state.

        ``finished`` also drops the whole-set run marker (the ticket has left the run); a
        yield keeps it. ``honor_takeover`` (finish only) skips the lease-owner check."""
        meta = self.get_fact(cid, space=space, snapshot=snapshot).get("meta") or {}
        held = meta.get(_ts.M_CLAIM_OWNER)
        if not honor_takeover and held is not None and held != owner:
            return None
        patch = {_ts.M_BUILD_STATE: state}
        for k in _ts._LEASE_KEYS:
            patch[k] = None
        if state == "finished":
            for k in _ts._RUN_KEYS:
                patch[k] = None
        self.patch_meta(cid, patch, space=space, snapshot=snapshot)
        return dict(patch)
