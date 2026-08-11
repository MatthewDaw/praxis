"""FL11 (R9/R10, KD9, E9/E10) — the human channel: a dedicated ``/af-learn`` skill callable from
any Claude session in any repo, wrapping the ingestion API (FL2's :func:`agent_factory.ingestion_api.ingest`)
on its lenient human channel. This module IS the skill's implementation — the SKILL.md at
``agent_factory/skills/af-learn/`` is the human-facing invocation contract that calls straight
into :func:`learn` / :func:`learn_bulk` below; nothing about drafting or writing lives twice.

KD9 — a dedicated skill, not an af-build-only path and not a bare MCP tool: ``/af-learn`` works
from any repo, so the very first thing it must do is resolve WHICH project space a complaint is
about (:func:`resolve_target_project`). There is no ambient "current project" the way af-build has
one — the caller must name it, explicitly or via the same ``FACTORY_PROJECT`` env seam
``hooks._super_run_identity`` already established for af-super-run, or this refuses (E9) rather
than guessing a target and writing into the wrong org's project.

KD5/DF4 — the human channel is LENIENT throughout: :func:`learn` calls ``ingest(..., channel="human")``,
which (per FL2/FL12) inserts even an unproven check as GATING immediately — proof is attempted and
recorded (``proven``/``unproven``) but never blocks insertion. R10's "loud" flag is exactly that
recorded ``proof_status`` field plus the check's participation in the KD7 re-prove cycle
(:func:`agent_factory.ingestion_api.reprove_quiet_checks`); its first real catch upgrades it to
``proven`` via :func:`agent_factory.ingestion_api.upgrade_on_first_pass`, which FL11 extended to
cover a GATING-but-unproven check (not only the machine REPORT_ONLY case FL12 already handled).

R9 — single AND bulk modes share one entry point shape: :func:`learn` files one complaint,
:func:`learn_bulk` files many "without per-check oversight" (E10) — no confirmation gate between
entries, a false-positive is caught downstream by R19's auto-suspension / the manual kill switch,
never by a review queue here (KD6 stays untouched). Both resolve the target project ONCE, before
any write, so a batch that cannot name a project space writes NOTHING (E9) rather than landing half
of it.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from agent_factory._hooks import _ticket_state as _ts

from agent_factory import ingestion_api

# The same seam ``hooks._super_run_identity`` already established for af-super-run (A2: reuse, don't
# invent a parallel one) — an explicit ``project`` argument always wins over this.
DEFAULT_PROJECT_ENV = "FACTORY_PROJECT"


class UnresolvableProjectSpace(ValueError):
    """E9 — ``/af-learn`` could not resolve a target project space (no explicit ``project`` and no
    ``FACTORY_PROJECT`` env var). Raised BEFORE any Praxis call, so a call that hits this writes
    nothing at all — never a lesson, never a check, never a ticket regression, and never a guess at
    a cross-org target."""


def resolve_target_project(project: str | None = None, *,
                           env: dict[str, str] | None = None) -> str:
    """Resolve the bare project name ``/af-learn`` writes into.

    An EXPLICIT ``project`` argument wins — this is the "naming the project ... in-session" E9
    requires when a caller cannot otherwise be sure which space a complaint belongs to. Absent
    that, the ``FACTORY_PROJECT`` env var (the same convention ``hooks._super_run_identity`` reads)
    is used. With neither, this REFUSES (:class:`UnresolvableProjectSpace`) rather than guessing —
    the skill-level contract is that the calling session confirms the target with the human before
    ever reaching this call when the project is ambiguous.

    Either source is normalized through :func:`hooks._ticket_state.project_ref`, which strips a
    leading ``prd-`` (possibly already present) so a caller may pass either the bare project name
    or its ``prd-<project>`` snapshot name and land on the same bare identity every other Praxis
    call in this codebase expects (never double-prefixed — the same footgun ``_super_run_identity``
    guards against)."""
    if project is not None and project.strip():
        return _ts.project_ref(project.strip()).plan[0]
    env_map = env if env is not None else os.environ
    env_project = str(env_map.get(DEFAULT_PROJECT_ENV) or "").strip()
    if env_project:
        return _ts.project_ref(env_project).plan[0]
    raise UnresolvableProjectSpace(
        "af-learn could not resolve a target project space: no explicit project was named and "
        f"no {DEFAULT_PROJECT_ENV} env var is set. Refusing to write anything (no lesson, no "
        "check, no ticket regression) or guess a cross-org target — name the project explicitly "
        "(E9)."
    )


def learn(complaint_text: str, project: str | None = None, *, source: str | None = None,
         drafted_run: str | None = None, drafted_rubric: dict[str, Any] | None = None,
         class_hint: str | None = None, ticket_ids: list[str] | None = None,
         surfaces: list[str] | None = None, drafting_transcript: str | None = None,
         proof_runner: Callable[[str], bool] | None = None, identity: str | None = None,
         env: dict[str, str] | None = None) -> dict[str, Any]:
    """R9 — the single-complaint entry point.

    Resolves the target project FIRST (:func:`resolve_target_project`; E9 — refuses and writes
    nothing when it cannot), then ingests via the ingestion API's lenient human channel
    (``channel="human"``): a lesson always lands (R2); a drafted check — if the caller (the skill,
    reasoning over the free-text complaint) supplies ``drafted_run``/``drafted_rubric`` — lands
    GATING immediately whether or not its fail-then-pass proof succeeded (KD5/DF4), with
    ``proof_status`` recorded (``proven``/``unproven``) so an unproven insert is visibly flagged
    (R10) rather than silently indistinguishable from a proven one. Any ``ticket_ids`` named are
    regressed in the SAME motion (R9's "in the same motion") — mandatorily pinned at their next
    claim via the ticket-identity lane (R11, resolved elsewhere in ``hooks._ticket_state``)."""
    target = resolve_target_project(project, env=env)
    return ingestion_api.ingest(
        complaint_text, target, source=source, drafted_run=drafted_run,
        drafted_rubric=drafted_rubric, channel="human", class_hint=class_hint,
        ticket_ids=ticket_ids, surfaces=surfaces, drafting_transcript=drafting_transcript,
        proof_runner=proof_runner, identity=identity,
    )


def learn_bulk(complaints: list[dict[str, Any]], project: str | None = None, *,
              identity: str | None = None, env: dict[str, str] | None = None,
              ) -> list[dict[str, Any]]:
    """R9/E10 — bulk mode: every requested check is inserted WITHOUT per-check oversight — one
    ``ingest`` call per complaint, all on the lenient human channel, with no confirmation gate
    between entries (a batch that would "immediately block many tickets" is caught downstream by
    R19's auto-suspension / the manual kill switch, never by pausing here).

    The target project is resolved ONCE, up front (:func:`resolve_target_project`), before any
    entry is written — a batch that cannot name a resolvable project space writes NOTHING (E9),
    never a partial landing of the first few entries followed by a refusal on a later one.

    Each entry in ``complaints`` is a dict shaped like :func:`learn`'s keyword arguments, with the
    complaint text under the key ``"complaint_text"`` (every other key is optional and passed
    straight through to :func:`agent_factory.ingestion_api.ingest`, e.g. ``drafted_run``,
    ``ticket_ids``, ``surfaces``). Returns one ingestion result dict per entry, in order."""
    target = resolve_target_project(project, env=env)
    results: list[dict[str, Any]] = []
    # Within-batch dedup guard: two identical lesson-only entries in ONE batch must not each write a
    # row. The corpus-exact dedup in ``ingest`` catches repeats across separate calls, but a fresh
    # write may not be read-back-consistent yet within the same batch (the vector index lag that let
    # the original repro produce 24 rows), so an in-memory key on the first entry's result is the
    # robust guard. Only lesson-ONLY entries are collapsed here; an entry that also drafts a check
    # runs through ``ingest`` in full so the check-authoring/proof/regress path (and its own
    # canonical-content-hash dedup) is never short-circuited.
    seen_lesson_only: dict[str, dict[str, Any]] = {}
    for entry in complaints:
        entry = dict(entry)
        text = entry.pop("complaint_text")
        # The verb-allowlist waiver is NOT caller-supplied data. `learn` deliberately exposes no
        # parameter that reaches `human_verbatim`, precisely because THIS module drafts run bodies
        # from free-text prose -- so a waiver granted here is self-granted, not human-reviewed.
        # Bulk mode splats its entries, which would have made one extra dict key the back door
        # around the whole verb allowlist. Refuse loudly rather than dropping it silently: a caller
        # who passed it believes the waiver is in effect, and a quiet strip would leave them wrong.
        if "human_verbatim" in entry:
            raise ValueError(
                "human_verbatim cannot be set from a bulk entry — the verb-allowlist waiver is an "
                "explicit operator decision, and af-learn drafts its run bodies from prose, so a "
                "waiver requested here would be granted by the drafter to itself"
            )
        drafts_check = entry.get("drafted_run") is not None or entry.get("drafted_rubric") is not None
        dedup_key = None if drafts_check else ingestion_api._normalize_lesson_text(text)
        if dedup_key and dedup_key in seen_lesson_only:
            prior = seen_lesson_only[dedup_key]
            # Same-batch twin: reuse the first entry's lesson id, write nothing, and mark it so the
            # caller can tell a collapsed duplicate apart from a fresh landing.
            results.append({**prior, "lesson_duplicate_of": prior.get("lesson_id"),
                            "batch_deduped": True})
            continue
        result = ingestion_api.ingest(text, target, channel="human", identity=identity, **entry)
        if dedup_key:
            seen_lesson_only[dedup_key] = result
        results.append(result)
    return results
