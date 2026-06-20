"""Local MCP server: a thin authenticated HTTP client over the Praxis backend.

Exposes two agent-facing tools (``praxis_get_context`` / ``praxis_add_insight``)
that call the backend ``/context`` and ``/insights`` endpoints with a cached
Cognito token (Bearer) + the active org (``X-Praxis-Org``). No DB access here —
tenancy is enforced server-side from the verified JWT + org membership.
"""
