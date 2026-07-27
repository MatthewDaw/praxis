"""af-super-run's project identity resolution — the ONE bare project name every Praxis call in
all three stages (af-plan, af-intake-plan, af-build) must use (B2 of the af-super-run requirements
doc, ``agent_factory/docs/brainstorms/2026-07-25-af-super-run-requirements.md``).

Priority, mirroring ``hooks/_gate_common.py``'s ``FACTORY_PROJECT`` seam (A2 — reuse, don't invent a
parallel one): an explicit ``--project`` argument, verbatim; else the ``FACTORY_PROJECT`` env var;
else a kebab-case slug derived from the idea string. Any name NOT sourced from the explicit argument
is recorded as a decision episode BEFORE the caller may perform any other Praxis write, so a resumed
or audited run can see how identity was derived. A run that cannot resolve a name from any of the
three sources REFUSES (returns ``None``) rather than inventing one mid-flight.

The resolved name is always BARE (a leading ``prd-`` stripped via ``_ticket_state.project_ref``, the
same stripping the completeness endpoints themselves rely on) — never pass a ``prd-``-prefixed name
to ``incomplete_requirements`` / ``build_completeness`` (F1: a double-prefixed source returns EMPTY
and fakes completeness).
"""

from __future__ import annotations

import os
import re
from typing import Callable, Optional

import _praxis
import _ticket_state as ts

_SLUG_RUN = re.compile(r"[^a-z0-9]+")


def _bare(name: str) -> str:
    """Strip a leading ``prd-`` (possibly repeated) via the same logic the plan/completeness lanes
    use, so this module can never itself introduce the double-prefix failure (F1)."""
    return ts.project_ref(name).plan[0]


def slugify(idea: str) -> str:
    """A deterministic kebab-case project-name slug derived from a rough idea string."""
    return _SLUG_RUN.sub("-", idea.strip().lower()).strip("-")


def resolve_project_identity(
    project_arg: Optional[str] = None,
    idea: Optional[str] = None,
    *,
    record_episode_fn: Optional[Callable[..., dict]] = None,
) -> Optional[str]:
    """Resolve the bare project name a super-run uses for every Praxis call in all three stages.

    Returns ``None`` (refuse to start) when none of the three sources resolve a name — the caller
    must not invent one. When the resolution did NOT come from the explicit ``project_arg`` (i.e. it
    fell through to ``FACTORY_PROJECT`` or the idea slug), the derived name is recorded as a decision
    episode (working memory — the project's own Praxis space may not exist yet at this point, see
    OD-3c) BEFORE returning, so no other Praxis write can precede it.
    """
    if project_arg is not None and project_arg.strip():
        return _bare(project_arg.strip())

    env_project = os.environ.get("FACTORY_PROJECT", "").strip()
    if env_project:
        resolved, source = _bare(env_project), "env:FACTORY_PROJECT"
    elif idea is not None and idea.strip() and slugify(idea):
        resolved, source = slugify(idea), "idea-slug"
    else:
        return None

    record = record_episode_fn or _praxis.record_episode
    record(
        f"af-super-run resolved project identity {resolved!r} from {source}",
        episode={"kind": "project-identity-resolution", "resolved": resolved, "source": source},
    )
    return resolved
