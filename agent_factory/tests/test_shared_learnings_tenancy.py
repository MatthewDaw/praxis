"""The shared learnings space must be genuinely SHARED across projects.

Every factory project runs in its own Praxis org with its own API key, and the server enforces
``keyOrg == requestedOrg``. Praxis's sharing primitive (``GET /org/sources``) is intra-org only. So
before this, a space named ``factory-learnings`` resolved under the ambient org was a DIFFERENT
space per project -- seven projects, seven isolated stores -- and the cross-project learning the
factory exists to perform silently never happened. Reproduced in the wild on the devbox: a loop
running under org ``sotos`` reported ``unknown space 'factory-learnings'``.

``FACTORY_LEARNINGS_ORG`` + ``FACTORY_LEARNINGS_API_KEY`` retarget ONLY the shared space, leaving
each project's tickets/checks/plan in its own org. The tests below pin all four properties that
matter, and the last two are the ones that keep the fix honest:

  1. unset -> byte-identical to the old behaviour (no project is disturbed);
  2. set   -> learnings traffic carries the shared tenancy;
  3. set   -> NON-learnings traffic still carries the project's own tenancy (a leak here would send
     a project's tickets to the shared org);
  4. org set with no key -> a LOUD, precise error rather than a 403 that
     ``not_a_factory_project`` would classify as a benign "no project here", which would make a
     missing credential look exactly like correct configuration and silently stop the sharing.
"""

from __future__ import annotations

import json
import pytest

from agent_factory._hooks import _praxis

LEARNINGS = _praxis.FACTORY_LEARNINGS_SPACE
SHARED_ORG = "praxis"
SHARED_KEY = "shared-key-123"


@pytest.fixture
def captured(monkeypatch):
    """Capture the headers of the next request instead of issuing it."""
    seen: dict[str, dict[str, str]] = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"ok": True}).encode()

    def fake_urlopen(req, timeout=None):
        seen["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return _Resp()

    monkeypatch.setattr(_praxis.urllib.request, "urlopen", fake_urlopen)
    # A project running under its own org with its own key -- the real-world starting state.
    monkeypatch.setenv("PRAXIS_ORG", "sports-analysis")
    monkeypatch.setenv("PRAXIS_API_KEY", "project-key-abc")
    monkeypatch.delenv("FACTORY_LEARNINGS_ORG", raising=False)
    monkeypatch.delenv("FACTORY_LEARNINGS_API_KEY", raising=False)
    return seen


def test_unset_leaves_learnings_traffic_exactly_as_it_was(captured):
    """Backward compatibility: no project that has not opted in changes behaviour."""
    _praxis._request("GET", "/facts/by", space=LEARNINGS, snapshot="lessons")
    assert captured["headers"]["x-praxis-org"] == "sports-analysis"
    assert captured["headers"]["x-praxis-key"] == "project-key-abc"


def test_learnings_traffic_uses_the_shared_tenancy(captured, monkeypatch):
    monkeypatch.setenv("FACTORY_LEARNINGS_ORG", SHARED_ORG)
    monkeypatch.setenv("FACTORY_LEARNINGS_API_KEY", SHARED_KEY)
    _praxis._request("GET", "/facts/by", space=LEARNINGS, snapshot="lessons")
    assert captured["headers"]["x-praxis-org"] == SHARED_ORG
    assert captured["headers"]["x-praxis-key"] == SHARED_KEY


@pytest.mark.parametrize("space,snapshot", [
    ("sports_analysis", "building-validation"),   # a project's checks
    ("sports_analysis", "prd-sports_analysis"),   # a project's plan
])
def test_project_traffic_never_leaks_into_the_shared_org(captured, monkeypatch, space, snapshot):
    """The override is scoped to ONE space. If it bled, a project's tickets would land in the
    shared org, which is a data-tenancy break, not merely a misroute."""
    monkeypatch.setenv("FACTORY_LEARNINGS_ORG", SHARED_ORG)
    monkeypatch.setenv("FACTORY_LEARNINGS_API_KEY", SHARED_KEY)
    _praxis._request("GET", "/facts/by", space=space, snapshot=snapshot)
    assert captured["headers"]["x-praxis-org"] == "sports-analysis"
    assert captured["headers"]["x-praxis-key"] == "project-key-abc"


def test_working_memory_traffic_is_untouched(captured, monkeypatch):
    """A request with no space is not snapshot-bound and must keep the ambient tenancy."""
    monkeypatch.setenv("FACTORY_LEARNINGS_ORG", SHARED_ORG)
    monkeypatch.setenv("FACTORY_LEARNINGS_API_KEY", SHARED_KEY)
    _praxis._request("GET", "/facts/by")
    assert captured["headers"]["x-praxis-org"] == "sports-analysis"


def test_a_missing_shared_key_fails_loud_not_as_a_benign_absence(captured, monkeypatch):
    """Without this the request 403s, `not_a_factory_project` reads that as "no project here", and
    a missing credential becomes indistinguishable from correct configuration."""
    monkeypatch.setenv("FACTORY_LEARNINGS_ORG", SHARED_ORG)
    with pytest.raises(_praxis.PraxisUnreachable, match="FACTORY_LEARNINGS_API_KEY"):
        _praxis._request("GET", "/facts/by", space=LEARNINGS, snapshot="lessons")


def test_no_key_is_fine_when_the_shared_org_is_already_the_ambient_one(captured, monkeypatch):
    """In the praxis repo itself the ambient key already belongs to the shared org; demanding a
    second one there would be pointless friction."""
    monkeypatch.setenv("PRAXIS_ORG", SHARED_ORG)
    monkeypatch.setenv("FACTORY_LEARNINGS_ORG", SHARED_ORG)
    _praxis._request("GET", "/facts/by", space=LEARNINGS, snapshot="lessons")
    assert captured["headers"]["x-praxis-org"] == SHARED_ORG
    assert captured["headers"]["x-praxis-key"] == "project-key-abc"


def test_flags_and_artifacts_share_the_same_space_and_so_the_same_tenancy(captured, monkeypatch):
    """Flags and proof artifacts live in the shared SPACE under their own snapshots, so they must
    ride the same credential -- otherwise `af-retro --flags` aggregates only the local org."""
    monkeypatch.setenv("FACTORY_LEARNINGS_ORG", SHARED_ORG)
    monkeypatch.setenv("FACTORY_LEARNINGS_API_KEY", SHARED_KEY)
    for snapshot in (_praxis.FACTORY_ARTIFACTS_SNAPSHOT, "flags"):
        _praxis._request("GET", "/facts/by", space=LEARNINGS, snapshot=snapshot)
        assert captured["headers"]["x-praxis-org"] == SHARED_ORG
