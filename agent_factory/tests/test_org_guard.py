"""B-6: ``assert_org_matches`` fails LOUD before a write when the org we are authenticated
against is not the org the caller expects — naming BOTH orgs.

Offline: a canned :class:`WhoAmI` is injected via the ``who`` kwarg, so no server or network
(``_request``) is touched at all.
"""

import sys
from pathlib import Path

import pytest

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _praxis  # noqa: E402


def _who(*, org, key_org, org_source="PRAXIS_ORG", auth_mode="key"):
    return _praxis.WhoAmI(
        backend="http://localhost:8000", org=org, org_source=org_source,
        principal="u", auth_mode=auth_mode, key_org=key_org, ok=True, detail="",
    )


def test_matching_org_returns_identity():
    who = _who(org="sports-analysis", key_org="sports-analysis")
    got = _praxis.assert_org_matches("sports-analysis", who=who)
    assert got is who


def test_drift_raises_and_names_both_orgs():
    # The real incident: data lives in 'sports-analysis' but the invocation is authenticated
    # against the empty 'taolu-coach' org (via a key scoped there).
    who = _who(org="taolu-coach", key_org="taolu-coach")
    with pytest.raises(_praxis.OrgMismatch) as exc:
        _praxis.assert_org_matches("sports-analysis", who=who)
    msg = str(exc.value)
    assert "sports-analysis" in msg  # the expected org
    assert "taolu-coach" in msg      # the org actually authenticated against
    # Subclass of PraxisUnreachable so existing fail-closed gates catch it.
    assert isinstance(exc.value, _praxis.PraxisUnreachable)


def test_key_org_is_ground_truth_over_resolved_org():
    # Resolved org would write to the default fallback, but the key is scoped elsewhere — the
    # org the write actually lands in is the key's, so that is what we compare.
    who = _who(org="agent-factory", key_org="taolu-coach", org_source="default")
    with pytest.raises(_praxis.OrgMismatch) as exc:
        _praxis.assert_org_matches("sports-analysis", who=who)
    assert "taolu-coach" in str(exc.value)


def test_bearer_mode_falls_back_to_resolved_org():
    # No key (bearer/dev) -> the request carries the resolved org, so that is the comparison.
    who = _who(org="sports-analysis", key_org=None, auth_mode="bearer")
    assert _praxis.assert_org_matches("sports-analysis", who=who) is who


def test_empty_expected_org_is_rejected():
    with pytest.raises(ValueError):
        _praxis.assert_org_matches("  ", who=_who(org="x", key_org="x"))
