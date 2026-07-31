"""Provision an isolated Praxis space for the eval, seed the planning checklist into it, and
read it back — all at execution time, through the real Praxis HTTP API.

Hermetic, repeatable lifecycle::

    create own space (POST /spaces, idempotent) -> drop the eval's snapshots (DELETE /snapshots,
    clean slate) -> seed the checklist (POST /insights, scoped by X-Praxis-Space +
    X-Praxis-Snapshot) -> read it back (GET /context, or facts_by when available)
    -> teardown (drop those snapshots again)

The eval relies on Praxis as the runtime store. The checklist *content* lives as a
version-controlled seed artifact (``planning-checklist.yaml``) but is exercised THROUGH a real
Praxis round-trip, never injected straight into the planner — that is what makes this a genuine
test of the Praxis-backed mechanism rather than a bypass. The seeded space is the eval's own
project space, isolated from every real project's graph, so the eval neither depends on nor
pollutes shared state.

Config via env (or inject a client): ``PRAXIS_BASE_URL``, ``PRAXIS_API_KEY``, ``PRAXIS_ORG``.

Teardown note: the API has no delete-the-space-record route, so teardown deletes the eval's
SNAPSHOTS; the empty space record persists and is reused next run. Teardown deliberately does NOT
call ``POST /graph/clear``: that route ignores the tenancy headers entirely and truncates the
*caller's private working memory* (see ``knowledge/serve/app.py::clear_graph``), so using it to
"clear the eval space" would destroy unrelated state belonging to whoever ran the eval.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

PLANNING_SCOPE = "planning"
CHECK_CATEGORY = "check"
# A ``scope="planning"`` CHECK is a planning LENS, and the server admits it into exactly ONE place:
# the project space's ``planning-validation`` snapshot (the snapshot-kind invariant in
# ``postgres_vector_graph._VALIDATION_SCOPE`` — ``building-validation`` takes only
# ``scope="validation"`` checks, a ``prd-*`` plan takes no checks at all). That is also where
# af-intake-plan RESOLVES lenses from (``_ticket_state.project_ref(p).planning == (p,
# "planning-validation")``). A check written without a ``(space, snapshot)`` target is a hard 400
# (``app._require_snapshot_for_check``) precisely because working memory is where no reader looks,
# so the header pair below is mandatory, not decoration.
PLANNING_CHECKS_SNAPSHOT = "planning-validation"
# The eval's own throwaway project. Under the canonical tenancy layout a project IS a space, so the
# eval's space must be named for the project af-intake-plan plans under — otherwise the lenses would
# be seeded into a space the pipeline never reads.
EVAL_PROJECT = "team-app-eval"
EVAL_SPACE_ID = EVAL_PROJECT
# The plan/ticket snapshot af-intake-plan writes the produced plan into, inside the same space.
EVAL_PLAN_SNAPSHOT = f"prd-{EVAL_PROJECT}"
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


def _build_space_client(space: str | None = None, snapshot: str | None = None) -> Any:
    """A ``praxis_client.PraxisClient`` bound to ``(space, snapshot)`` via the tenancy header PAIR,
    extended with the space-admin op the thin client doesn't expose (create).

    FAIL-CLOSED, mirroring the server and ``hooks/_praxis``: a snapshot-bound op emits BOTH
    ``X-Praxis-Space`` and ``X-Praxis-Snapshot`` or neither. Exactly one of them is a
    misconfiguration — the server silently ignores the lone header and routes the op to the
    caller's working memory, which is invisible to every factory reader — so refuse it here.
    Passing neither is the working-memory client used for the space-admin calls.
    """
    if (space is None) != (snapshot is None):
        raise ValueError(
            f"partial snapshot reference (space={space!r}, snapshot={snapshot!r}) — pass both or "
            "neither; a lone header is ignored by the server and the op lands in working memory"
        )
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
            if space and snapshot:
                headers["X-Praxis-Space"] = space
                headers["X-Praxis-Snapshot"] = snapshot
            return headers

        def create_space(self, space_id: str, name: str = "") -> dict[str, Any]:
            try:
                return self._request("POST", "/spaces", body={"spaceId": space_id, "name": name})
            except PraxisError as exc:
                if getattr(exc, "status_code", None) == 409:  # already exists -> fine
                    return {"spaceId": space_id, "existed": True}
                raise

    return _SpaceClient(base, key, org)


# --- seed artifact -------------------------------------------------------------


def load_seed_checklist(path: str | Path = DEFAULT_CHECKLIST_ARTIFACT) -> list[str]:
    """Load the checklist seed artifact (the content the eval writes into its own space)."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    items = data.get("checks") if isinstance(data, dict) else data
    return [t for t in (str(c).strip() for c in (items or [])) if t]


# --- lifecycle: provision (create+clear+seed+read) / teardown ------------------


def _drop_eval_snapshots(client: Any, space_id: str) -> None:
    """Delete the eval's lens + plan snapshots (idempotent; a missing snapshot deletes zero rows).

    This is the clean-slate primitive at BOTH ends of the lifecycle. It replaces the old
    ``clear_graph()`` call, which never touched these snapshots at all (it truncates the caller's
    working memory regardless of headers) — so with snapshot-bound seeding, dropping the snapshot is
    also what keeps a reused space from accumulating a duplicate lens set on every run.
    """
    for snapshot in (PLANNING_CHECKS_SNAPSHOT, EVAL_PLAN_SNAPSHOT):
        client.delete_snapshot(space_id, snapshot)


def provision_and_load_checklist(
    *,
    space_id: str = EVAL_SPACE_ID,
    artifact: str | Path = DEFAULT_CHECKLIST_ARTIFACT,
    client: Any = None,
) -> list[str]:
    """Create the eval's own Praxis space, clear it, seed the checklist, and read it back.

    The checklist is seeded into ``(space_id, planning-validation)`` — the only snapshot the server
    admits ``scope="planning"`` checks into, and the one af-intake-plan resolves its lenses from.

    Returns the checklist as loaded *from Praxis* (the round-trip), not the raw artifact, so a
    seed/store/retrieve failure surfaces as a short/empty checklist rather than passing silently.
    """
    checks = load_seed_checklist(artifact)
    if client is None:
        _build_space_client().create_space(space_id)  # create (working-memory client, no target)
        # Bound to the lens snapshot for seed + read-back: a category="check" write with no
        # (space, snapshot) target is a 400, and one with only a space header lands in working
        # memory where no reader looks.
        client = _build_space_client(space=space_id, snapshot=PLANNING_CHECKS_SNAPSHOT)
    else:
        client.create_space(space_id)
    _drop_eval_snapshots(client, space_id)  # clean slate — a reused space starts empty every run
    for text in checks:
        # No ``on_conflict``: a ``category="check"`` write is routed server-side to the
        # identity-keyed check upsert on a redact-only graph — no dedup, no merge, no
        # contradiction step — so the argument was inert and only implied a guard that
        # does not exist here.
        client.add_insight(
            text,
            category=CHECK_CATEGORY,
            scope=PLANNING_SCOPE,
            source=SEED_SOURCE,
        )
    return load_planning_checklist(client=client)


def teardown_eval_space(*, space_id: str = EVAL_SPACE_ID, client: Any = None) -> None:
    """Drop the eval's snapshots (the only teardown the API offers — the space record persists)."""
    client = client or _build_space_client(
        space=space_id, snapshot=PLANNING_CHECKS_SNAPSHOT
    )
    _drop_eval_snapshots(client, space_id)


# --- read ----------------------------------------------------------------------


def load_planning_checklist(
    client: Any = None,
    *,
    scope: str = PLANNING_SCOPE,
    category: str = CHECK_CATEGORY,
    top_k: int = 200,
) -> list[str]:
    """Return the planning checklist (each check's criterion text) from the eval's lens snapshot.

    Prefers an exhaustive ``facts_by`` enumeration when the client exposes it; otherwise falls
    back to filtered semantic ``get_context`` (a top-k sample — adequate for a small seeded
    checklist, not for thousands; see ``docs/coverage-spine/01-praxis-changes.md`` G1). Either way
    the read is bound to ``(EVAL_SPACE_ID, planning-validation)`` by the client's header pair, so it
    reads back exactly what was seeded.
    """
    if client is None:
        client = _build_space_client(
            space=EVAL_SPACE_ID, snapshot=PLANNING_CHECKS_SNAPSHOT
        )

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
