"""S13: unforgeable caller context — a build worker cannot obtain the attested path by naming it.

The ``record_validation_pass`` function derives the effective ``source`` from an unforgeable
execution property (a credential env var), not from a self-declared parameter. A build worker
passing ``source="human"`` without the credential is still refused; only a caller presenting
the distinct credential records an attested pass.
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
    """Persists ONE ticket's meta across calls; ``patch_meta`` MERGES like the real server."""

    def __init__(self, meta):
        self._meta = dict(meta)

    def get_fact(self, cid, *, space=None, snapshot=None):
        return {"id": cid, "meta": dict(self._meta)}

    def patch_meta(self, cid, meta_dict, *, space=None, snapshot=None):
        self._meta.update(meta_dict)
        return {"id": cid, "meta": dict(self._meta)}


def _install(monkeypatch, meta):
    fake = FakePraxis(meta)
    monkeypatch.setattr(ts, "_praxis", fake)
    monkeypatch.setattr(ts._praxis, "facts_by", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(ts._praxis, "surface_checks", lambda *a, **k: [], raising=False)
    # This test is scoped to caller-context forgery, not the (separately-tested) universal lane.
    monkeypatch.setattr(ts, "_universal_checks", lambda: [])
    return fake


def test_worker_supplying_human_source_is_refused(monkeypatch):
    """A build worker that supplies source="human" by name gets source="worker" — refused."""
    fake = _install(monkeypatch, {
        "requirement_id": "R1", "tags": [],
        "acceptance": "UX feels instant", "verify": "manual",
    })

    # Ensure the attestation credential is NOT set.
    monkeypatch.delenv("PRAXIS_ATTESTED_CALLER", raising=False)

    # 1) Start the ticket.
    ts.start_ticket("R1", "owner", project="team-app")
    assert "R1::acceptance" in fake._meta[ts.M_MANUAL_REQUIREMENTS]

    # 2) Worker authors + pins a covering validation and tries to record with source="human".
    ts.pin_validations("R1", [{"validation_id": "v1", "covers": ["R1::acceptance"],
                               "run": "echo ok"}], ref=PLAN)
    ts.record_validation_pass("R1", "v1", True, source="human", ref=PLAN)

    # 3) The effective source MUST be "worker" (not "human") — the worker cannot self-attest.
    # (Pinned may also carry an auto-authored universal-lane entry (R33) alongside "v1" — that
    # lane's own coverage is not what this test is about.)
    pinned = (fake._meta.get(ts.M_PINNED_CHECKS) or [])
    assert any(e.get("validation_id") == "v1" for e in pinned)
    effective_source = pinned[0].get("source")
    assert effective_source == "worker", (
        f"Expected source='worker' (refused self-attest), got '{effective_source}'"
    )

    # 4) With worker source, a manual requirement is NOT satisfied.
    assert ts.all_validations_passed("R1", ref=PLAN) is False

    # 5) Now set the attestation credential and re-record.
    monkeypatch.setenv("PRAXIS_ATTESTED_CALLER", "true")
    ts.record_validation_pass("R1", "v1", True, source="human", ref=PLAN)

    # 6) With the credential, the human source IS honored.
    pinned = (fake._meta.get(ts.M_PINNED_CHECKS) or [])
    effective_source = pinned[0].get("source")
    assert effective_source == "human", (
        f"Expected source='human' (credentialed attestation), got '{effective_source}'"
    )

    # 7) With attested source, the manual requirement IS satisfied.
    assert ts.all_validations_passed("R1", ref=PLAN) is True


def test_worker_source_still_works_for_automated(monkeypatch):
    """A worker-run pass (default source) still satisfies an automated ticket."""
    fake = _install(monkeypatch, {
        "requirement_id": "R2", "tags": [], "acceptance": "returns 200",
    })
    monkeypatch.delenv("PRAXIS_ATTESTED_CALLER", raising=False)

    ts.start_ticket("R2", "owner", project="team-app")
    assert fake._meta[ts.M_MANUAL_REQUIREMENTS] == []
    assert "R2::acceptance" in fake._meta[ts.M_REQUIRED_VALIDATIONS]

    ts.pin_validations("R2", [{"validation_id": "v1", "covers": ["R2::acceptance"],
                               "run": "curl -s -o /dev/null -w '%{http_code}'"}], ref=PLAN)
    ts.record_validation_pass("R2", "v1", True, ref=PLAN)  # default worker source

    assert ts.all_validations_passed("R2", ref=PLAN) is True


def test_default_source_is_worker(monkeypatch):
    """When no source is passed, the effective source defaults to 'worker'."""
    fake = _install(monkeypatch, {
        "requirement_id": "R3", "tags": [], "acceptance": "thing works",
    })
    monkeypatch.delenv("PRAXIS_ATTESTED_CALLER", raising=False)

    ts.start_ticket("R3", "owner", project="team-app")
    ts.pin_validations("R3", [{"validation_id": "v1", "covers": ["R3::acceptance"],
                               "run": "true"}], ref=PLAN)
    ts.record_validation_pass("R3", "v1", True, ref=PLAN)  # no source arg

    pinned = (fake._meta.get(ts.M_PINNED_CHECKS) or [])
    assert pinned[0].get("source") == "worker"


def test_attested_credential_required_for_all_human_sources(monkeypatch):
    """ALL values in HUMAN_PASS_SOURCES require the credential — not just 'human'."""
    for bad_source in ts.HUMAN_PASS_SOURCES:
        fake = _install(monkeypatch, {
            "requirement_id": f"R-{bad_source}", "tags": [],
            "acceptance": "UX feels instant", "verify": "manual",
        })
        monkeypatch.delenv("PRAXIS_ATTESTED_CALLER", raising=False)
        cid = f"R-{bad_source}"

        ts.start_ticket(cid, "owner", project="team-app")
        ts.pin_validations(cid, [{"validation_id": "v1",
                                   "covers": [f"{cid}::acceptance"],
                                   "run": "echo ok"}], ref=PLAN)
        ts.record_validation_pass(cid, "v1", True, source=bad_source, ref=PLAN)

        pinned = (fake._meta.get(ts.M_PINNED_CHECKS) or [])
        effective = pinned[0].get("source")
        assert effective == "worker", (
            f"source='{bad_source}' without credential should be forced to 'worker', got '{effective}'"
        )
        assert ts.all_validations_passed(cid, ref=PLAN) is False
