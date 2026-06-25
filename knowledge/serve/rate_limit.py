"""App-layer rate limiting for the public FastAPI backend (``knowledge.serve``).

The backend is the real public MCP surface (App Runner, ``mcp.praxiskg.com``); the
stdio MCP server is a single-user local client and needs no limiting. Limits are
keyed on the authenticated *principal*, not the client IP:

* clients sit behind the App Runner proxy, so the remote address is unreliable; and
* the MCP client mints a FRESH Cognito JWT on EVERY request
  (``knowledge.mcp.identity.token`` calls ``renew_access_token`` each call), so the
  raw bearer string changes per request — keying on the token would defeat limiting.

:func:`principal_key` therefore buckets on a STABLE identifier: the ``X-Praxis-Key``
header (one value per API key) if present, else the ``sub`` claim decoded from the
bearer JWT *without signature verification* (a cheap bucketing key only — real auth
still verifies the token downstream), else the remote address as a last resort.

Storage is slowapi's default in-memory store: per-instance only. The current
deployment is a single App Runner instance, so this is sufficient; if the service
ever scales horizontally the buckets would need a shared backend (e.g. Redis) so a
client can't get N× its quota by spreading requests across instances.
"""

from __future__ import annotations

import jwt
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

# Tight bucket for the LLM-cost / paid routes (each fires OpenRouter calls):
# /insights, /ingest, /ingest/session, /fold-in, /evals/regenerate, /evals/load,
# /context. Kept generous for now: the deployment is small/low-profile and
# OpenRouter has a $10/day hard cap, so the cost blast radius is bounded — these
# limits exist mainly to stop runaway loops, not to ration normal iteration.
LLM_RATE_LIMIT = "30/minute"

# Looser global default for every other (cheap) route.
GLOBAL_RATE_LIMIT = "180/minute"


def principal_key(request: Request) -> str:
    """Stable per-principal bucket key (see module docstring).

    Order: X-Praxis-Key header -> unverified JWT ``sub`` -> remote address.
    """
    api_key = request.headers.get("x-praxis-key")
    if api_key:
        return f"key:{api_key.strip()}"

    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        try:
            # Unverified decode: we only need the stable ``sub`` to bucket on; the
            # signature is verified for real downstream in serve.auth.verify_token.
            claims = jwt.decode(token, options={"verify_signature": False})
            sub = claims.get("sub")
            if sub:
                return f"sub:{sub}"
        except Exception:  # noqa: BLE001 - a malformed token just falls through
            pass

    return f"ip:{get_remote_address(request)}"


def build_limiter() -> Limiter:
    """A Limiter keyed on the principal, with the global default applied to all routes.

    Per-route tight limits are layered on via ``@limiter.limit(LLM_RATE_LIMIT)``
    decorators in ``app.py``; ``/health`` is exempted there.
    """
    return Limiter(key_func=principal_key, default_limits=[GLOBAL_RATE_LIMIT])
