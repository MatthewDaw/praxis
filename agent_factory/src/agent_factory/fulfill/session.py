"""U6 — session lifecycle + seeding.

A fulfill session is a per-taxpayer Praxis SPACE (KTD5). Snapshots are space-scoped and cannot serve
as a cross-session template (proven by probe), so the runtime SEEDS a fresh space by ingesting the
domain's requirement files as ``category="requirement"`` facts (``source="prd-<project>"``) carrying
the pack meta, then binds each rendering requirement's ``renders`` edge to the single deliverable
surface (D9). After seeding, the two gates read as expected: ``completeness_summary`` = ``0/N`` and
``surface_coverage`` = ``0 uncovered``.

Per-session space lifecycle/TTL is owned by Praxis (Q7): :meth:`Session.close` is the explicit
teardown hook, not an automatic cleanup policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .domain import Domain
from .praxis_client import FulfillPraxis

# The requirement meta keys carried from the pack onto the seeded Praxis fact.
_META_KEYS = ("field", "verify", "cover", "renders", "depends_on", "guard", "scope")


def _requirement_meta(req: dict) -> dict[str, Any]:
    """Build the seed fact's ``meta`` from a pack requirement (requirement_id + the pack keys present)."""
    meta: dict[str, Any] = {"requirement_id": str(req.get("id"))}
    for key in _META_KEYS:
        if key in req and req[key] is not None:
            meta[key] = req[key]
    return meta


@dataclass
class Session:
    """A live fulfill session: its Praxis space, the domain, the client, and the seeded fact ids."""

    session_id: str
    space_id: str
    domain: Domain
    client: FulfillPraxis
    requirement_fact_ids: dict[str, str] = field(default_factory=dict)

    @property
    def project(self) -> str:
        return self.domain.project

    def close(self) -> None:
        """Explicit teardown hook (Q7 — Praxis owns lifecycle). Best-effort space deletion."""
        try:
            self.client.delete_space(self.space_id)
        except Exception:  # noqa: BLE001 — teardown must not raise into a caller's finally
            pass


def space_id_for(session_id: str) -> str:
    """The space id for a session id. Praxis space slugs are lowercase ``[a-z0-9_-]``."""
    slug = "".join(c if (c.isalnum() or c in "-_") else "-" for c in session_id.lower())
    return f"sess-{slug}"


def start_session(
    domain: Domain,
    session_id: str,
    *,
    client: FulfillPraxis | None = None,
    create_space: bool = True,
) -> Session:
    """Create + seed a fresh session space for ``domain``.

    Creates the space ``sess-<session_id>`` (unless ``create_space=False``, for an externally
    provisioned space), ingests every pack requirement as a ``category="requirement"`` fact under
    ``source="prd-<project>"`` with the pack meta, ensures the deliverable surface, and binds each
    requirement with a non-empty ``renders`` list onto it. Fail-closed: any Praxis error during
    seeding propagates (a half-seeded session is never returned as usable).
    """
    space = space_id_for(session_id)
    px = client or FulfillPraxis(space=space)
    px.space = space  # ensure every call this session makes is scoped to its space

    if create_space:
        px.create_space(space, name=f"{domain.id} :: {session_id}")

    screen_id = domain.deliverable_screen_id
    px.ensure_surface(domain.project, screen_id, title=domain.deliverable_label)

    fact_ids: dict[str, str] = {}
    for req in domain.requirements:
        rid = str(req.get("id"))
        fact_id = px.ingest_requirement(
            text=str(req.get("text") or rid),
            source=domain.source,
            scope=req.get("scope"),
            meta=_requirement_meta(req),
        )
        fact_ids[rid] = fact_id
        if req.get("renders"):  # only rendering requirements bind to the deliverable surface
            px.bind_surface(fact_id, screen_id, domain.project, title=domain.deliverable_label)

    return Session(
        session_id=session_id,
        space_id=space,
        domain=domain,
        client=px,
        requirement_fact_ids=fact_ids,
    )
