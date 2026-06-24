"""API keys create -> list -> revoke flow against the mock provider + panel view."""

from __future__ import annotations

from components import PANELS
from components.api_keys_panel import (
    build_api_keys_view,
    create_api_key,
    revoke_api_key,
)
from models.api_key import ApiKey, CreatedApiKey
from services.contract_v1 import build_create_api_key_body, parse_api_key_list
from services.mock_provider import MockDataProvider


def test_mock_create_returns_raw_key_once() -> None:
    provider = MockDataProvider()
    created = provider.create_api_key("laptop CLI")
    assert isinstance(created, CreatedApiKey)
    assert created.key.startswith("pxk_")
    assert created.label == "laptop CLI"

    # The raw key is never echoed by the list endpoint.
    listed = provider.list_api_keys()
    assert all(not hasattr(k, "key") for k in listed)


def test_create_list_revoke_flow() -> None:
    provider = MockDataProvider()
    assert provider.list_api_keys() == []

    created = create_api_key(provider, "ci")
    keys = provider.list_api_keys()
    assert len(keys) == 1
    assert keys[0].id == created.id
    assert keys[0].revoked is False
    assert keys[0].status == "active"

    revoked = revoke_api_key(provider, created.id)
    assert isinstance(revoked, ApiKey)
    assert revoked.revoked is True
    assert revoked.status == "revoked"
    assert provider.list_api_keys()[0].revoked is True


def test_panel_view_marks_revoked_rows_non_revocable() -> None:
    provider = MockDataProvider()
    active = create_api_key(provider, "active-key")
    dead = create_api_key(provider, "dead-key")
    revoke_api_key(provider, dead.id)

    view = build_api_keys_view(provider)
    assert view.columns == ("Label", "Created", "Last used", "Status")
    assert not view.is_empty

    by_id = {r.id: r for r in view.rows}
    assert by_id[active.id].can_revoke is True
    assert by_id[active.id].last_used == "never"
    assert by_id[dead.id].can_revoke is False
    assert by_id[dead.id].status == "revoked"


def test_revoke_unknown_key_raises() -> None:
    provider = MockDataProvider()
    try:
        revoke_api_key(provider, "nope")
    except KeyError:
        pass
    else:  # pragma: no cover
        raise AssertionError("revoking an unknown key should raise KeyError")


def test_create_body_normalizes_blank_label_to_null() -> None:
    assert build_create_api_key_body(label=None) == {"label": None}
    assert build_create_api_key_body(label="   ") == {"label": None}
    assert build_create_api_key_body(label=" team ") == {"label": "team"}


def test_parse_api_key_list_accepts_array_and_wrapped() -> None:
    rows = [{"id": "key_1", "userId": "u", "createdAt": "t", "revoked": False}]
    assert parse_api_key_list(rows) == rows
    assert parse_api_key_list({"apiKeys": rows}) == rows
    assert parse_api_key_list({"unexpected": 1}) == []


def test_panel_registry_exposes_api_keys() -> None:
    assert "api_keys" in PANELS
