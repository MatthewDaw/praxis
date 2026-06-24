"""Dashboard UI components (framework-agnostic view models + optional Streamlit render).

Panels are registered here so any host (Streamlit, a thin Python shell, or the
React bridge) discovers them through one import surface. Each entry maps a stable
panel key to its render entrypoint.
"""

from __future__ import annotations

from components.api_keys_panel import (
    build_api_keys_view,
    render_api_keys_panel,
)

# Registry of dashboard panels: key -> Streamlit render entrypoint.
# Mirrors the nav ordering the host renders.
PANELS = {
    "api_keys": render_api_keys_panel,
}

__all__ = [
    "PANELS",
    "build_api_keys_view",
    "render_api_keys_panel",
]
