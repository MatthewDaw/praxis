"""Monica-owned read-only proxy for the Arize Phoenix REST API.

The dashboard's Candidate Detail card needs the Phoenix trace(s) tied to a
candidate, but the Phoenix API requires a Bearer key and lives on a different
origin. Shipping the key in the static React bundle is unsafe, so this small
FastAPI app holds the key server-side, calls Phoenix, and returns a normalized,
secret-free shape the frontend can render.

This service is intentionally separate from ``knowledge/serve`` (Matthew's
candidate API) so the dashboard pillar can ship trace context without touching
the shared pipeline/API.
"""

from frontend.phoenix_proxy.app import app, create_app

__all__ = ["app", "create_app"]
