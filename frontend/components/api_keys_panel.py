"""
===============================================================================
FILE: components/api_keys_panel.py

PURPOSE:
"API keys" management panel for the human-gate dashboard. Lets a user create a
key (raw ``pxk_`` value shown ONCE), list existing keys, and revoke active ones.

DESIGN:
Mirrors the dashboard's UI-agnostic component convention: the view model
(``build_api_keys_view`` + row/cell formatting) is pure and unit-testable
against any DataProvider, and a thin ``render_api_keys_panel`` draws it with
Streamlit when that optional dependency is installed. The provider (HTTP or
mock) is the single source of truth for the create -> list -> revoke flow.

SECURITY:
- The raw key (``CreatedApiKey.key``) is only ever held transiently to show the
  one-time reveal box; it is never persisted to the keys table.
===============================================================================
"""

from __future__ import annotations

from dataclasses import dataclass

from models.api_key import ApiKey, CreatedApiKey
from services.data_provider import DataProvider

_TABLE_COLUMNS = ("Label", "Created", "Last used", "Status")

REVEAL_WARNING = (
    "Copy this key now. For your security it will not be shown again."
)


@dataclass(frozen=True)
class ApiKeyRow:
    """One rendered row of the keys table."""

    id: str
    label: str
    created: str
    last_used: str
    status: str
    revoked: bool

    @property
    def can_revoke(self) -> bool:
        return not self.revoked


@dataclass(frozen=True)
class ApiKeysView:
    """Everything the panel needs to render the keys table."""

    columns: tuple[str, ...]
    rows: list[ApiKeyRow]

    @property
    def is_empty(self) -> bool:
        return not self.rows


def _row_from_key(key: ApiKey) -> ApiKeyRow:
    return ApiKeyRow(
        id=key.id,
        label=key.label or "(unlabeled)",
        created=key.created_at or "-",
        last_used=key.last_used_at or "never",
        status=key.status,
        revoked=key.revoked,
    )


def build_api_keys_view(provider: DataProvider) -> ApiKeysView:
    """Read the caller's keys and shape them into a render-ready table."""
    rows = [_row_from_key(key) for key in provider.list_api_keys()]
    return ApiKeysView(columns=_TABLE_COLUMNS, rows=rows)


def create_api_key(provider: DataProvider, label: str | None = None) -> CreatedApiKey:
    """Create a key; caller is responsible for the one-time reveal."""
    return provider.create_api_key(label=label)


def revoke_api_key(provider: DataProvider, key_id: str) -> ApiKey:
    """Revoke an active key by id."""
    return provider.revoke_api_key(key_id)


def render_api_keys_panel(provider: DataProvider) -> None:  # pragma: no cover - UI glue
    """Draw the panel with Streamlit (no-op import guard if it is unavailable)."""
    try:
        import streamlit as st
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "render_api_keys_panel requires streamlit; use build_api_keys_view "
            "for headless/React clients."
        ) from exc

    st.subheader("API keys")
    st.caption(
        "Programmatic access uses these keys. They are scoped to your user and org."
    )

    with st.form("create_api_key"):
        label = st.text_input("Label (optional)", placeholder="e.g. laptop CLI")
        submitted = st.form_submit_button("Create API key")
    if submitted:
        created = create_api_key(provider, label or None)
        st.session_state["_new_api_key"] = created.key

    new_key = st.session_state.get("_new_api_key")
    if new_key:
        st.success("API key created")
        st.code(new_key, language=None)
        st.warning(REVEAL_WARNING)
        if st.button("I've copied it", key="dismiss_new_api_key"):
            st.session_state.pop("_new_api_key", None)

    view = build_api_keys_view(provider)
    if view.is_empty:
        st.info("No API keys yet. Create one above.")
        return

    header = st.columns(len(view.columns) + 1)
    for col, name in zip(header, view.columns):
        col.markdown(f"**{name}**")
    header[-1].markdown("**Action**")

    for row in view.rows:
        cells = st.columns(len(view.columns) + 1)
        cells[0].write(row.label)
        cells[1].write(row.created)
        cells[2].write(row.last_used)
        cells[3].write(row.status)
        if row.can_revoke:
            if cells[-1].button("Revoke", key=f"revoke_{row.id}"):
                revoke_api_key(provider, row.id)
                st.rerun()
        else:
            cells[-1].write("-")
