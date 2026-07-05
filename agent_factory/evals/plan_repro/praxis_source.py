"""Provision an isolated Praxis space for the eval, seed the planning checklist into it, and
read it back — all at execution time, through the real Praxis HTTP API.

Hermetic, repeatable lifecycle::

    create own space (POST /spaces, idempotent) -> clear it (POST /graph/clear, clean slate)
    -> seed the checklist (POST /insights, scoped by X-Praxis-Space) -> read it back
    (GET /context, or facts_by when available) -> teardown (clear the space's graph)

The eval relies on Praxis as the runtime store. The checklist *content* lives as a
version-controlled seed artifact (``planning-checklist.yaml``) but is exercised THROUGH a real
Praxis round-trip, never injected straight into the planner — that is what makes this a genuine
test of the Praxis-backed mechanism rather than a bypass. The seeded space is isolated from the
default / ``prd-team-app`` graph, so the eval neither depends on nor pollutes shared state.

Config via env (or inject a client): ``PRAXIS_BASE_URL``, ``PRAXIS_API_KEY``, ``PRAXIS_ORG``.

Teardown note: the API exposes create/list spaces and ``POST /graph/clear`` (empties the
space's graph, scoped by ``X-Praxis-Space``) but no delete-the-space-record route, so teardown
clears the graph; the empty space record persists and is reused next run.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

PLANNING_SCOPE = "planning"
CHECK_CATEGORY = "check"
EVAL_SPACE_ID = "eval-plan-repro"
SEED_SOURCE = "eval-planning-checklist"
DEFAULT_CHECKLIST_ARTIFACT = Path(__file__).resolve().parent / "planning-checklist.yaml"


# --- client (praxis_client extended with space ops) ----------------------------


def _env_config() -> tuple[str, str, str]:
    base = os.environ.get("PRAXIS_BASE_URL")
    key = os.environ.get("PRAXIS_API_KEY")
    org = os.environ.get("PRAXIS_ORG", "agent-factory")
    if not base or not key:  # pragma: no cover - config-dependent
        raise RuntimeError(
            "set PRAXIS_BASE_URL and PRAXIS_API_KEY (and optionally PRAXIS_ORG) to use Praxis"
        )
    return base, key, org


def _build_space_client(space: str | None = None) -> Any:
    """A ``praxis_client.PraxisClient`` scoped to ``space`` (via ``X-Praxis-Space``), extended
    with the space-admin ops the thin client doesn't expose (create + clear)."""
    try:
        from praxis_client import PraxisClient, PraxisError
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            "praxis_client not importable — `pip install -e ../praxis` or add ../praxis to "
            "PYTHONPATH, or inject a client."
        ) from exc
    base, key, org = _env_config()

    class _SpaceClient(PraxisClient):
        def _headers(self) -> dict[str, str]:
            headers = super()._headers()
            if space:
                headers["X-Praxis-Space"] = space
            return headers

        def create_space(self, space_id: str, name: str = "") -> dict[str, Any]:
            try:
                return self._request("POST", "/spaces", body={"spaceId": space_id, "name": name})
            except PraxisError as exc:
                if getattr(exc, "status_code", None) == 409:  # already exists -> fine
                    return {"spaceId": space_id, "existed": True}
                raise

        def clear_graph(self) -> dict[str, Any]:
            return self._request("POST", "/graph/clear", body=None)

    return _SpaceClient(base, key, org)


# --- seed artifact -------------------------------------------------------------


def load_seed_checklist(path: str | Path = DEFAULT_CHECKLIST_ARTIFACT) -> list[str]:
    """Load the checklist seed artifact (the content the eval writes into its own space)."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    items = data.get("checks") if isinstance(data, dict) else data
    return [t for t in (str(c).strip() for c in (items or [])) if t]


# --- lifecycle: provision (create+clear+seed+read) / teardown ------------------


def provision_and_load_checklist(
    *,
    space_id: str = EVAL_SPACE_ID,
    artifact: str | Path = DEFAULT_CHECKLIST_ARTIFACT,
    client: Any = None,
) -> list[str]:
    """Create the eval's own Praxis space, clear it, seed the checklist, and read it back.

    Returns the checklist as loaded *from Praxis* (the round-trip), not the raw artifact, so a
    seed/store/retrieve failure surfaces as a short/empty checklist rather than passing silently.
    """
    checks = load_seed_checklist(artifact)
    if client is None:
        _build_space_client(space=None).create_space(space_id)  # create (default scope)
        client = _build_space_client(space=space_id)            # scoped for clear/seed/read
    else:
        client.create_space(space_id)
    client.clear_graph()  # clean slate — a reused space starts empty every run
    for text in checks:
        client.add_insight(
            text,
            category=CHECK_CATEGORY,
            scope=PLANNING_SCOPE,
            source=SEED_SOURCE,
            on_conflict="auto_resolve",
        )
    return load_planning_checklist(client=client)


def teardown_eval_space(*, space_id: str = EVAL_SPACE_ID, client: Any = None) -> None:
    """Empty the eval's space (the only teardown the API offers — the record persists)."""
    client = client or _build_space_client(space=space_id)
    client.clear_graph()


# --- read ----------------------------------------------------------------------


def load_planning_checklist(
    client: Any = None,
    *,
    scope: str = PLANNING_SCOPE,
    category: str = CHECK_CATEGORY,
    top_k: int = 200,
) -> list[str]:
    """Return the planning checklist (each check's criterion text) from the Praxis space.

    Prefers an exhaustive ``facts_by`` enumeration when the client exposes it; otherwise falls
    back to filtered semantic ``get_context`` (a top-k sample — adequate for a small seeded
    checklist, not for thousands; see ``docs/coverage-spine/01-praxis-changes.md`` G1).
    """
    if client is None:
        client = _build_space_client(space=EVAL_SPACE_ID)

    if hasattr(client, "facts_by"):  # exhaustive structured enumeration (preferred)
        payload = client.facts_by(category=category, scope=scope)
        hits = payload.get("facts") or payload.get("hits") or []
        return [t for t in (str(h.get("text", "")).strip() for h in hits) if t]

    payload = client.get_context(
        f"{scope} {category}: considerations to enforce when planning", top_k=top_k
    )
    hits = payload.get("hits") or []
    return [
        text
        for h in hits
        if h.get("category") == category
        and h.get("scope") == scope
        and (text := str(h.get("text", "")).strip())
    ]
