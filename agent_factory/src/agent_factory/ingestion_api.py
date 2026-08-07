"""The ingestion API — the sole writer of validation/knowledge content (FL1/FL2, KD1/KD3/KD8).

FL1 laid the foundation: a shared org-level ``factory-learnings`` space holds lessons, cloud-
canonical (Praxis, never a local file), mounted READ-ONLY into every project space at claim/
resolve time (see ``hooks._ticket_state.start_ticket``). :func:`write_lesson` is the sole write
path into it; :func:`read_lessons` is a plain GET.

FL2 adds the full ingestion SEQUENCE and its five sibling verbs (R1/R1a/R1b/R4/KD8): classify/
dedup a lesson against the existing corpus, draft a check (allowlist-validated when machine-
drafted, hash-pinned at insertion), attempt a fail-then-pass proof, bind it at the narrowest
scope, activate it, and regress the matching tickets — all as ONE call (:func:`ingest`). The
five siblings are ``widen``/``suspend``/``kill_switch``/``regress``/``reclassify``; every one of
the resulting six verbs refuses an unauthenticated caller BEFORE any write (:func:`_require_authenticated`,
R1b). :func:`execute_check` refuses to run a check whose live content has drifted from its
insertion-time hash pin (KD8 anchor 1). :func:`rollback_wave` is the named rollback unit (D9/E14).
:func:`plan_time_author_check` / :func:`plan_time_author_lens` are R1a's lenient plan-time entry
point — exempt from the lesson/proof requirements, for completeness guards and doc-sync checks
that have no failure to prove against.

FL3 adds the failure-class taxonomy (R3) and the R20b staged-calibration singleton state, sharing
this module's write path via :func:`_write_insight`.

FL4 (R7) extends this same sole-writer module with the bad-artifact pin: at regression time the
failing commit is bundled into a SELF-CONTAINED git reproduction bundle plus secret-scanned
diff/evidence text, written into the ``artifacts`` snapshot of the same shared space
(:func:`pin_artifact`), so proof and future re-proof (KD7) always have something to run against.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hooks import _praxis

from agent_factory.rubric import rubric_from_dict, rubric_to_dict

LESSON_CATEGORY = "lesson"
CHECK_CATEGORY = "check"
BUILDING_VALIDATION_SNAPSHOT = "building-validation"
PLANNING_VALIDATION_SNAPSHOT = "planning-validation"
CLASS_CATEGORY = "failure-class"          # FL3 — the failure-class taxonomy (R3)
CALIBRATION_CATEGORY = "taxonomy-calibration"  # FL3 — the R20b staged-rollout singleton state
ARTIFACT_CATEGORY = "artifact"            # FL4 — the pinned bad-artifact bundle (R7)
DEFAULT_RETENTION_DAYS = 90
DEFAULT_REPEAT_COUNT = 1        # FL5/D4: proof repeat count for flaky/LLM-judged checks; 1 == no repeat
DEFAULT_REDRAFT_BUDGET = 3      # FL5/D1: machine-drafting attempts before lesson-only
PROOF_CHECK_UNDRAFTABLE = "check-undraftable"  # FL5/R6: redraft budget exhausted, no gating check inserted

# R20a's check enforcement-state machine (a plan-only spec until FL2; this is its first code home).
M_ENFORCEMENT_STATE = "enforcement_state"
STATE_GATING = "gating"
STATE_REPORT_ONLY = "report_only"
STATE_SUSPENDED = "suspended"
STATE_ARCHIVED = "archived"

# KD8 anchor 4: a machine-drafted run body must validate against a declared command
# template/allowlist at schema-validation time — evidence-steered drafting cannot smuggle an
# arbitrary command that hash-pinning would then legitimize. A human-authored (plan-time or
# lenient-channel) run body is exempt — a human already reviewed it.
RUN_BODY_ALLOWED_PREFIXES = (
    "pytest", "python -m", "python3 -m", "npm test", "npm run", "npx playwright",
    "make ", "grep ", "ruff ", "mypy ", "eslint ", "playwright test",
)
_RUN_BODY_FORBIDDEN_TOKENS = (";", "&&", "|", "`", "$(", ">", "<")


class RunBodyRejected(ValueError):
    """A machine-drafted run body falls outside the declared command allowlist (KD8 anchor 4)."""


class CheckContentDrifted(ValueError):
    """A check's live content no longer matches its insertion-time hash pin (KD8 anchor 1)."""


class Unauthenticated(PermissionError):
    """An ingestion-API verb was called with no org-authenticated Praxis identity (R1b)."""


def _write_insight(text: str, category: str, *, source: str | None = None,
                   meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Shared write path: POST /insights, scoped to the shared ``factory-learnings`` space.

    THE sole write path into that space (R1/KD3): nothing else in this codebase is allowed to
    target ``(hooks._praxis.FACTORY_LEARNINGS_SPACE, hooks._praxis.FACTORY_LEARNINGS_SNAPSHOT)``
    with a write. Idempotently bootstraps the space on first use (a space that has never been
    created 404s on its first snapshot-bound write). Returns the server's insight-write ack
    (``{"summary","action","id",...}``).
    """
    body = str(text or "").strip()
    if not body:
        raise ValueError("text is required")
    _praxis.ensure_space(_praxis.FACTORY_LEARNINGS_SPACE, name="factory-learnings")
    payload: dict[str, Any] = {
        "insight": body,
        "category": category,
        "source": source,
        "meta": meta or {},
    }
    return _praxis._request(
        "POST", "/insights", body=payload,
        space=_praxis.FACTORY_LEARNINGS_SPACE, snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT,
    )


def write_lesson(text: str, *, source: str | None = None,
                 meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write ``text`` as a lesson into the shared ``factory-learnings`` space (POST /insights)."""
    return _write_insight(text, LESSON_CATEGORY, source=source, meta=meta)


def read_lessons(query: str = "", *, top_k: int = 10) -> list[dict[str, Any]]:
    """Read-only lessons lookup against the shared ``factory-learnings`` space.

    A GET-only call (``context``/``facts_by``): there is no write side-effect, so this is safe to
    call from any project session without granting it a write path. With a non-empty ``query`` this
    is similarity-ranked (top ``top_k``); with an empty query it is the exhaustive active-lesson
    enumeration.
    """
    q = (query or "").strip()
    if q:
        return _praxis.context(q, top_k=top_k, space=_praxis.FACTORY_LEARNINGS_SPACE,
                               snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT)
    return _praxis.facts_by(category=LESSON_CATEGORY, space=_praxis.FACTORY_LEARNINGS_SPACE,
                            snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT)


# --------------------------------------------------------------------------- R1b: auth gate

def _require_authenticated(identity: str | None = None) -> str:
    """Refuse a verb call BEFORE any write when no org-authenticated identity is available.

    ``identity`` lets an already-authenticated caller (e.g. an MCP tool that resolved its own
    principal) pass it straight through with no extra round trip; omitted, this asks Praxis
    ``whoami`` and refuses (``Unauthenticated``) on anything short of an ``ok`` answer with a real
    principal. Every one of the six verbs calls this as its FIRST statement (R1b)."""
    ident = (identity or "").strip()
    if ident:
        return ident
    try:
        who = _praxis.whoami()
    except _praxis.PraxisUnreachable as exc:
        raise Unauthenticated(
            f"ingestion API refused: identity could not be verified ({exc})"
        ) from exc
    if not who.ok or not who.principal or who.principal == "?":
        raise Unauthenticated(
            f"ingestion API refused an unauthenticated call: "
            f"{who.detail or 'no org-authenticated identity'}"
        )
    return who.principal


# --------------------------------------------------------------------------- KD8: allowlist + hash pins

def _validate_run_body(run: str, *, channel: str) -> str:
    """Validate a drafted check's run body (KD8 anchor 4). A ``machine`` channel body must start
    with a declared command template and carry no shell-metacharacter escape; a ``human`` channel
    body (plan-time / lenient) is exempt — already reviewed."""
    body = str(run or "").strip()
    if not body:
        raise RunBodyRejected("run body is empty")
    if channel == "machine":
        if not body.startswith(RUN_BODY_ALLOWED_PREFIXES):
            raise RunBodyRejected(
                f"machine-drafted run body {body!r} is outside the declared command "
                f"template/allowlist {RUN_BODY_ALLOWED_PREFIXES!r}"
            )
        for token in _RUN_BODY_FORBIDDEN_TOKENS:
            if token in body:
                raise RunBodyRejected(
                    f"machine-drafted run body {body!r} contains disallowed shell token {token!r}"
                )
    return body


def _hash_text(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _hash_rubric(rubric: dict[str, Any]) -> str:
    """Canonical rubric-JSON hash (sorted keys — order-independent, byte-stable)."""
    return _hash_text(json.dumps(rubric or {}, sort_keys=True))


def verify_pin(check: dict[str, Any]) -> None:
    """Refuse (raise :class:`CheckContentDrifted`) when a check's LIVE content no longer matches
    its insertion-time hash pin — binary checks by ``run`` body hash, graded checks by canonical
    rubric-JSON hash (KD8 anchor 1). Called by :func:`execute_check` before it ever runs anything."""
    meta = check.get("meta") or {}
    check_id = meta.get("check_id", "<unknown>")
    if meta.get("kind") == "graded":
        pinned, live, label = meta.get("rubric_hash"), _hash_rubric(meta.get("rubric") or {}), "rubric JSON"
    else:
        pinned, live, label = meta.get("run_hash"), _hash_text(meta.get("run") or ""), "run body"
    if not pinned:
        raise CheckContentDrifted(f"check {check_id!r} has no {label} hash pin recorded")
    if live != pinned:
        raise CheckContentDrifted(
            f"check {check_id!r} {label} content hash drifted from its insertion-time pin "
            f"(pinned={pinned[:12]}… live={live[:12]}…) — refusing to execute"
        )


def execute_check(check: dict[str, Any], *, runner: Callable[[str], bool] | None = None) -> bool:
    """Run a BINARY check's proof AFTER verifying its content hash pin; never executes drifted
    content. ``runner`` defaults to a real shell execution (injected for tests)."""
    verify_pin(check)
    meta = check.get("meta") or {}
    if meta.get("kind") == "graded":
        raise ValueError("execute_check runs binary checks only; graded checks are judge-scored")
    run = str(meta.get("run") or "")
    do_run = runner or _default_shell_runner
    return bool(do_run(run))


def _default_shell_runner(run: str) -> bool:  # pragma: no cover - real subprocess, exercised via injection in tests
    return subprocess.run(run, shell=True, check=False).returncode == 0


# --------------------------------------------------------------------------- R7/KD8: secret redaction

_SECRET_KV_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b(\s*[:=]\s*)['\"]?"
    r"[A-Za-z0-9/_\-.+=]{6,}['\"]?"
)
_SECRET_LITERAL_RES = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),  # FL4: all GitHub token kinds, not just ghp_
    re.compile(r"AKIA[0-9A-Z]{16}"),
)
_SECRET_BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9\-_.]{10,}")


def redact_secrets(text: str) -> str:
    """Targeted secret redaction (R7): replace a credential-shaped substring with ``[REDACTED]``
    wherever a cloud-written artifact (provenance, drafting transcript, pinned-artifact diff/
    evidence text — FL4) is stored — never a blanket wipe, so the surrounding prose (and
    non-secret evidence) survives."""
    out = str(text or "")
    out = _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", out)
    for pat in _SECRET_LITERAL_RES:
        out = pat.sub("[REDACTED]", out)
    out = _SECRET_BEARER_RE.sub(r"\1[REDACTED]", out)
    return out


# --------------------------------------------------------------------------- checks (building-validation / planning-validation)

def write_check(criterion: str, project: str, *, meta: dict[str, Any],
                source: str | None = None,
                snapshot: str = BUILDING_VALIDATION_SNAPSHOT) -> dict[str, Any]:
    """Write ONE ``category="check"`` fact into ``(project, snapshot)`` — the same section-locked
    write shape :func:`write_lesson` uses for the shared learnings space, retargeted at a
    project's own checks snapshot. THE write path every check-authoring verb below funnels through."""
    body = str(criterion or "").strip()
    if not body:
        raise ValueError("criterion is required")
    payload: dict[str, Any] = {
        "insight": body, "category": CHECK_CATEGORY, "source": source, "meta": dict(meta or {}),
    }
    return _praxis._request("POST", "/insights", body=payload, space=project, snapshot=snapshot)


def _fetch_check(check_id: str, project: str) -> dict[str, Any]:
    matches = _praxis.facts_by(category=CHECK_CATEGORY, meta={"check_id": check_id},
                               space=project, snapshot=BUILDING_VALIDATION_SNAPSHOT)
    if not matches:
        raise ValueError(f"no check with check_id={check_id!r} in {project}/{BUILDING_VALIDATION_SNAPSHOT}")
    return matches[0]


# --------------------------------------------------------------------------- classify/dedup

def classify_and_dedup(lesson_text: str, *, class_hint: str | None = None,
                       top_k: int = 5) -> dict[str, Any]:
    """R1's first step: classify a new lesson against the existing corpus and flag an exact-text
    duplicate. Filed under ``class_hint`` (or ``"uncategorized"``) when no hint is given."""
    text_n = str(lesson_text or "").strip().lower()
    duplicate_of = None
    if text_n:
        for hit in read_lessons(lesson_text, top_k=top_k):
            if str(hit.get("text") or "").strip().lower() == text_n:
                duplicate_of = hit.get("id")
                break
    return {"class": (class_hint or "uncategorized"), "duplicate_of": duplicate_of}


def _pin_content(meta: dict[str, Any], *, validated_run: str | None,
                 rubric: dict[str, Any] | None) -> None:
    """Hash-pin an already-validated check content (binary ``validated_run`` XOR ``rubric``) into
    ``meta`` at insertion (KD8 anchor 1) — the ONE place :func:`ingest` and
    :func:`plan_time_author_check` both go through, so the pin scheme never has to be kept in
    sync by hand across the two authoring paths. Does NOT validate: the caller runs
    :func:`_validate_run_body` itself, once, before any write (KD8 anchor 4)."""
    if rubric is not None:
        rubric_dict = rubric_to_dict(rubric_from_dict(rubric))
        meta["kind"] = "graded"
        meta["rubric"] = rubric_dict
        meta["rubric_hash"] = _hash_rubric(rubric_dict)
    elif validated_run is not None:
        meta["run"] = validated_run
        meta["run_hash"] = _hash_text(validated_run)


def attempt_proof(run: str | None, *, proof_runner: Callable[[str], bool] | None = None) -> str:
    """Attempt a fail-then-pass proof of a drafted binary check (R1/R6). With no ``proof_runner``
    injected (the default — a merge-time caller wires a real bad→good disposable-worktree
    runner, R7) the check lands ``"unproven"`` rather than pretending a proof happened."""
    if not run or proof_runner is None:
        return "unproven"
    try:
        return "proven" if proof_runner(run) else "unproven"
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        # A crashing bad->good runner (missing worktree, non-zero-but-not-a-verdict tooling
        # failure) is simply an unproven check, not a fatal ingestion error.
        return "unproven"


# --------------------------------------------------------------------------- the six verbs (R1/R1b)

def ingest(lesson_text: str, project: str, *, source: str | None = None,
          drafted_run: str | None = None, drafted_rubric: dict[str, Any] | None = None,
          channel: str = "machine", class_hint: str | None = None,
          ticket_ids: list[str] | None = None, surfaces: list[str] | None = None,
          drafting_transcript: str | None = None,
          proof_runner: Callable[[str], bool] | None = None,
          identity: str | None = None,
          bad_artifact_meta: dict[str, Any] | None = None,
          healthy_repo_path: str | Path | None = None, healthy_ref: str = "HEAD",
          repeat_count: int = DEFAULT_REPEAT_COUNT, redraft_budget: int = DEFAULT_REDRAFT_BUDGET,
          run_candidates: list[str] | None = None,
          proof_executor: Callable[[str, Path], bool] | None = None) -> dict[str, Any]:
    """R1 — the full ingestion sequence, as one call: classify/dedup → write lesson →
    draft check (allowlist-validated when machine-drafted, hash-pinned at insertion) → attempt
    a fail-then-pass proof → bind at the narrowest scope → activate → regress the
    matching tickets → record provenance (drafting transcript secret-scanned, R7). Refuses
    (``Unauthenticated``) before any write when the caller is not org-authenticated (R1b).

    FL5/R6/R7: when both ``bad_artifact_meta`` (the FL4 pin) and ``healthy_repo_path`` are given,
    proof runs for real via :func:`attempt_fail_then_pass_proof` — a disposable-isolated-worktree,
    both-sides-executed, bounded-redraft proof — instead of the FL2 single-shot ``proof_runner``
    placeholder. A ``check-undraftable`` verdict (redraft budget exhausted, still vacuous) inserts
    NO check at all: the lesson lands alone, flagged (R6)."""
    authenticated_as = _require_authenticated(identity)

    use_real_proof_engine = (
        drafted_rubric is None and drafted_run is not None
        and bad_artifact_meta is not None and healthy_repo_path is not None
    )

    # Validate the drafted run body ONCE, BEFORE any write — "rejected at insertion (never
    # written)" must hold for the LESSON too, not just the check: a rejected draft leaves
    # nothing behind. The validated (stripped) body is reused below so it is never re-validated.
    validated_run: str | None = None
    validated_candidates: list[str] = []
    if use_real_proof_engine:
        raw_candidates = list(run_candidates or [])
        if drafted_run not in raw_candidates:
            raw_candidates.insert(0, drafted_run)
        validated_candidates = [_validate_run_body(c, channel=channel) for c in raw_candidates]
    elif drafted_run is not None and drafted_rubric is None:
        validated_run = _validate_run_body(drafted_run, channel=channel)

    classification = classify_and_dedup(lesson_text, class_hint=class_hint)
    wave_id = uuid.uuid4().hex

    lesson = write_lesson(lesson_text, source=source, meta={
        "class": classification["class"], "duplicate_of": classification["duplicate_of"],
        "wave_id": wave_id, "channel": channel, "authored_by": authenticated_as,
    })
    lesson_id = lesson.get("id")

    check_id: str | None = None
    proof_status: str | None = None
    proof_result: dict[str, Any] | None = None
    if use_real_proof_engine:
        proof_result = attempt_fail_then_pass_proof(
            validated_candidates, bad_artifact_meta=bad_artifact_meta,
            healthy_repo_path=healthy_repo_path, healthy_ref=healthy_ref,
            repeat_count=repeat_count, redraft_budget=redraft_budget, executor=proof_executor,
        )
        proof_status = proof_result["status"]
        if proof_status == PROOF_CHECK_UNDRAFTABLE:
            _praxis.patch_meta(
                lesson_id,
                {"check_undraftable": True, "check_undraftable_reason": proof_result["reason"]},
                space=_praxis.FACTORY_LEARNINGS_SPACE, snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT,
            )
            _praxis.record_episode(
                f"check-undraftable for lesson {lesson_id} (project={project}): exhausted the "
                f"redraft budget ({proof_result['attempts']} attempt(s)) with no valid "
                f"fail-then-pass proof — no gating check inserted.",
                outcome="failure",
            )
            return {"lesson_id": lesson_id, "check_id": None, "wave_id": wave_id,
                    "proof_status": proof_status, "class": classification["class"]}
        validated_run = proof_result["run"]

    if drafted_run is not None or drafted_rubric is not None:
        applies_to = list(ticket_ids or [])
        surface_only = not applies_to and bool(surfaces)  # R12: zero-match ingestion binds surface-only
        check_meta: dict[str, Any] = {
            "check_id": f"fl-{wave_id[:12]}", "scope": "validation", "wave_id": wave_id,
            "channel": channel, "applies_to": applies_to, "surfaces": list(surfaces or []),
            "surface_only": surface_only, "lesson_id": lesson_id, "source_evidence": source,
            "authored_by": authenticated_as,
        }
        _pin_content(check_meta, validated_run=validated_run, rubric=drafted_rubric)
        if proof_status is None:
            if drafted_rubric is not None:
                proof_status = "unproven"  # graded checks are judge-scored, not fail-then-pass proof-run
            else:
                proof_status = attempt_proof(validated_run, proof_runner=proof_runner)

        check_meta["proof_status"] = proof_status
        if proof_result is not None:
            check_meta["proof_reason"] = proof_result.get("reason")
            check_meta["proof_attempts"] = proof_result.get("attempts")
        if drafting_transcript is not None:
            check_meta["drafting_transcript"] = redact_secrets(drafting_transcript)
        # activate: a proven machine check (or any human-authored one) gates; unproven -> report_only.
        check_meta[M_ENFORCEMENT_STATE] = (
            STATE_GATING if (proof_status == "proven" or channel == "human") else STATE_REPORT_ONLY
        )

        written_check = write_check(lesson_text, project, meta=check_meta, source=source)
        check_id = written_check.get("id")

        if proof_result is not None and proof_result.get("flag"):
            _praxis.record_episode(
                f"proof flagged for check {check_id} (lesson {lesson_id}, project={project}): "
                f"{proof_result.get('reason')} — {proof_result.get('detail', '')}".strip(" —"),
                outcome="pending",
            )

        if ticket_ids:
            _praxis.regress_requirements(
                project, ticket_ids,
                detail={tid: {"regression_detail": {"source": "ingestion-api",
                                                     "lesson_id": lesson_id, "check_id": check_id}}
                        for tid in ticket_ids},
            )

    return {"lesson_id": lesson_id, "check_id": check_id, "wave_id": wave_id,
            "proof_status": proof_status, "class": classification["class"]}


def _patch_check(check_id: str, project: str,
                 build_patch: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]], *,
                 identity: str | None = None) -> dict[str, Any]:
    """Auth-gate, look up, and patch ONE check's meta — the shared plumbing behind
    :func:`widen`/:func:`suspend`/:func:`kill_switch`, which differ only in their patch payload.
    ``build_patch`` is either a ready-made patch dict, or a ``(check) -> patch`` callable for a
    verb (``widen``) that needs the fetched check's current meta to compute its patch."""
    authenticated_as = _require_authenticated(identity)
    check = _fetch_check(check_id, project)
    patch = build_patch(check) if callable(build_patch) else dict(build_patch)
    patch["patched_by"] = authenticated_as
    return _praxis.patch_meta(check["id"], patch, space=project, snapshot=BUILDING_VALIDATION_SNAPSHOT)


def widen(check_id: str, project: str, new_applies_to: list[str], *,
         reason: str | None = None, identity: str | None = None) -> dict[str, Any]:
    """R17/KD7 — widen a check's scope (e.g. on cross-scope recurrence with fresh proof)."""
    def _widen_patch(check: dict[str, Any]) -> dict[str, Any]:
        widened = sorted(set((check.get("meta") or {}).get("applies_to") or []) | set(new_applies_to or []))
        return {"applies_to": widened, "widened_at": time.time(), "widen_reason": reason}
    return _patch_check(check_id, project, _widen_patch, identity=identity)


def suspend(check_id: str, project: str, reason: str, *, identity: str | None = None) -> dict[str, Any]:
    """R19 — the automatic false-positive signal: stops gating, resurrectable."""
    patch = {M_ENFORCEMENT_STATE: STATE_SUSPENDED, "suspend_reason": str(reason or ""),
             "suspended_at": time.time()}
    return _patch_check(check_id, project, patch, identity=identity)


def kill_switch(check_id: str, project: str, reason: str, *, identity: str | None = None) -> dict[str, Any]:
    """R19 — the manual, immediate disable (distinct entry point from the automatic
    false-positive suspension; same resulting state, always recorded with a reason)."""
    patch = {M_ENFORCEMENT_STATE: STATE_SUSPENDED, "kill_switch": True,
             "kill_switch_reason": str(reason or ""), "kill_switch_at": time.time()}
    return _patch_check(check_id, project, patch, identity=identity)


def regress(project: str, ticket_ids: list[str], *, detail: dict[str, Any] | None = None,
           identity: str | None = None) -> dict[str, Any]:
    """Regress the matched ticket set through the ingestion API's own auth gate (R1/R5)."""
    _require_authenticated(identity)
    return _praxis.regress_requirements(project, ticket_ids, detail=detail)


def reclassify(lesson_id: str, new_class: str, *, reason: str | None = None,
              identity: str | None = None) -> dict[str, Any]:
    """Moves a lesson to a named class and records the correction (append-only history —
    never overwritten, so a lesson's classification path stays auditable)."""
    authenticated_as = _require_authenticated(identity)
    fact = _praxis.get_fact(lesson_id, space=_praxis.FACTORY_LEARNINGS_SPACE,
                            snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT)
    meta = fact.get("meta") or {}
    corrections = list(meta.get("corrections") or [])
    corrections.append({"from": meta.get("class"), "to": new_class, "reason": reason,
                        "by": authenticated_as, "at": time.time()})
    patch = {"class": new_class, "corrections": corrections}
    return _praxis.patch_meta(lesson_id, patch, space=_praxis.FACTORY_LEARNINGS_SPACE,
                              snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT)


# --------------------------------------------------------------------------- D9/E14: the rollback unit

def rollback_wave(wave_id: str, project: str, *, identity: str | None = None) -> dict[str, Any]:
    """The named rollback unit for one ingestion wave, in a single command: every check the wave
    wrote is deactivated (archived) and every lesson it wrote is annotated — no per-fact
    manual undo."""
    authenticated_as = _require_authenticated(identity)
    checks = _praxis.facts_by(category=CHECK_CATEGORY, meta={"wave_id": wave_id},
                              space=project, snapshot=BUILDING_VALIDATION_SNAPSHOT)
    lessons = _praxis.facts_by(category=LESSON_CATEGORY, meta={"wave_id": wave_id},
                               space=_praxis.FACTORY_LEARNINGS_SPACE,
                               snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT)
    now = time.time()
    deactivated = []
    for check in checks:
        _praxis.patch_meta(check["id"], {M_ENFORCEMENT_STATE: STATE_ARCHIVED, "rolled_back_at": now,
                                         "rolled_back_by": authenticated_as},
                           space=project, snapshot=BUILDING_VALIDATION_SNAPSHOT)
        deactivated.append(check["id"])
    annotated = []
    for lesson in lessons:
        _praxis.patch_meta(lesson["id"], {"rolled_back": True, "rolled_back_at": now,
                                          "rolled_back_by": authenticated_as},
                           space=_praxis.FACTORY_LEARNINGS_SPACE,
                           snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT)
        annotated.append(lesson["id"])
    return {"wave_id": wave_id, "checks_deactivated": deactivated, "lessons_annotated": annotated}


# --------------------------------------------------------------------------- R1a: plan-time entry point

def plan_time_author_check(check_text: str, project: str, *, applies_to: list[str] | None = None,
                           run: str | None = None, rubric: dict[str, Any] | None = None,
                           surfaces: list[str] | None = None, source: str | None = None,
                           identity: str | None = None) -> dict[str, Any]:
    """R1a — the lenient plan-time authoring entry point: exempt from the lesson/proof
    requirements (writes NO lesson, attempts NO proof) for completeness guards and doc-sync
    checks that have no failure to prove against. Still requires an org-authenticated identity
    (R1b) and still hash-pins its content at insertion (KD8 anchor 1 applies regardless of
    channel)."""
    authenticated_as = _require_authenticated(identity)
    meta: dict[str, Any] = {
        "check_id": f"plan-{uuid.uuid4().hex[:12]}", "scope": "validation", "channel": "human",
        "applies_to": list(applies_to or ["*"]), "surfaces": list(surfaces or []),
        M_ENFORCEMENT_STATE: STATE_GATING, "proof_status": "exempt", "authored_by": authenticated_as,
    }
    validated_run = _validate_run_body(run, channel="human") if (run is not None and rubric is None) else None
    _pin_content(meta, validated_run=validated_run, rubric=rubric)
    return write_check(check_text, project, meta=meta, source=source)


def plan_time_author_lens(lens_text: str, project: str, *, applies_to: list[str] | None = None,
                          source: str | None = None, identity: str | None = None) -> dict[str, Any]:
    """R1a's planning-lens sibling: writes a ``planning-validation`` lens and re-arms the
    blessing audit by recording a fresh, non-stale decision-log episode — the same re-arm
    signal ``af-intake-plan-validation`` uses — so the audit panel must reconvene to close it."""
    authenticated_as = _require_authenticated(identity)
    written = write_check(lens_text, project, source=source,
                          meta={"check_id": f"lens-{uuid.uuid4().hex[:12]}", "scope": "planning",
                                "applies_to": list(applies_to or ["*"]), "authored_by": authenticated_as},
                          snapshot=PLANNING_VALIDATION_SNAPSHOT)
    _praxis.record_episode(
        f"Re-armed prd-{project} plan audit: planning checklist extended with lens "
        f"{written.get('id')} ({lens_text[:80]}); prior panel-ran is stale and the audit must "
        f"reconvene to close the new lens.",
        outcome="pending",
    )
    return written


# --------------------------------------------------------------------------- FL3 taxonomy + calibration
# The dedup MATCHING logic and calibration MATH live in ``agent_factory.failure_taxonomy`` — this
# module stays the sole writer/reader of the shared space; that module never touches ``_praxis``.

def write_class(label: str, *, source: str | None = None,
                meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Mint a new failure-class fact (R3: a genuinely novel failure)."""
    return _write_insight(label, CLASS_CATEGORY, source=source, meta=meta)


def read_classes() -> list[dict[str, Any]]:
    """Read-only enumeration of every failure class (GET-only, no write side-effect)."""
    return _praxis.facts_by(category=CLASS_CATEGORY, space=_praxis.FACTORY_LEARNINGS_SPACE,
                            snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT)


def update_class_meta(class_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Merge ``meta`` onto an existing class fact (recurrence count + evidence log) — a recurrence
    NEVER writes a duplicate lesson, it only updates the class it matched."""
    return _praxis.patch_meta(class_id, meta, space=_praxis.FACTORY_LEARNINGS_SPACE,
                              snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT)


def read_calibration_state() -> dict[str, Any] | None:
    """The singleton R20b calibration-state fact, or ``None`` before the first assignment ever
    recorded. Read-only."""
    hits = _praxis.facts_by(category=CALIBRATION_CATEGORY, space=_praxis.FACTORY_LEARNINGS_SPACE,
                            snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT)
    return hits[0] if hits else None


def write_calibration_state(meta: dict[str, Any]) -> dict[str, Any]:
    """Create-or-update the singleton calibration-state fact. First call mints it; every later call
    merges the new counters onto the same fact, so there is always exactly one."""
    existing = read_calibration_state()
    if existing is not None:
        return _praxis.patch_meta(existing["id"], meta, space=_praxis.FACTORY_LEARNINGS_SPACE,
                                  snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT)
    return _write_insight("failure-class taxonomy calibration state", CALIBRATION_CATEGORY, meta=meta)


# --------------------------------------------------------------------------- FL4: bad-artifact pin (R7)
# redact_secrets (above) already covers this section's diff/evidence-text scrubbing needs; its
# pattern set was widened for FL4 rather than duplicated (see the R7/KD8 section).

def build_repro_bundle(repo_path: str | Path, commit_sha: str) -> bytes:
    """A SELF-CONTAINED git bundle reproducing ``commit_sha`` and its full ancestry (R7): bundled
    under a throwaway ref so it re-materializes the failing tree via ``git clone`` on a machine
    that never held ``repo_path``'s loose objects (worktree branches are reaped and their commits
    GC-eligible once the ticket ships). The throwaway ref is created and deleted around the bundle
    call so ``repo_path`` — which may be the live project checkout — is left exactly as found.
    """
    repo_path = str(repo_path)
    ref = f"refs/heads/repro-pin-{uuid.uuid4().hex}"
    subprocess.run(["git", "-C", repo_path, "update-ref", ref, commit_sha],
                   check=True, capture_output=True, text=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_path = Path(tmp) / "repro.bundle"
            subprocess.run(["git", "-C", repo_path, "bundle", "create", str(bundle_path), ref],
                           check=True, capture_output=True, text=True)
            return bundle_path.read_bytes()
    finally:
        subprocess.run(["git", "-C", repo_path, "update-ref", "-d", ref],
                       check=True, capture_output=True, text=True)


def materialize_bundle(bundle_bytes: bytes, dest_dir: str | Path) -> Path:
    """Re-materialize a pinned bundle's failing tree by cloning it into ``dest_dir`` — the step
    proof/re-proof execution runs against (R7), pointed at a disposable directory that never
    touched the origin repo's objects. Returns the checked-out clone's path."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = dest_dir / "_repro.bundle"
    bundle_path.write_bytes(bundle_bytes)
    clone_dir = dest_dir / "repro"
    ref = _bundle_ref_name(bundle_path)
    subprocess.run(["git", "clone", "--quiet", "--branch", ref, str(bundle_path), str(clone_dir)],
                   check=True, capture_output=True, text=True)
    return clone_dir


def _bundle_ref_name(bundle_path: Path) -> str:
    """The short branch name a :func:`build_repro_bundle` bundle carries, read back via ``git
    bundle list-heads`` so :func:`materialize_bundle` never has to guess/hardcode the throwaway
    ref (``git clone --branch`` wants the short name, not the full ``refs/heads/...`` path)."""
    out = subprocess.run(["git", "bundle", "list-heads", str(bundle_path)],
                         check=True, capture_output=True, text=True).stdout
    first_line = out.strip().splitlines()[0]  # "<sha> refs/heads/repro-pin-<uuid>"
    full_ref = first_line.split(" ", 1)[1].strip()
    return full_ref.removeprefix("refs/heads/")


def pin_artifact(*, project: str, ticket_id: str, commit_sha: str, repo_path: str | Path,
                 diff_text: str = "", evidence_text: str = "", while_gating: bool = True,
                 retention_days: int = DEFAULT_RETENTION_DAYS,
                 source: str | None = None) -> dict[str, Any]:
    """Pin the bad artifact at regression time (R7): a self-contained reproduction bundle over
    ``commit_sha`` plus secret-scanned diff/evidence text, written into cloud storage (the shared
    space's ``artifacts`` snapshot) retained per the default policy — kept while its check is
    gating, else expiring ``retention_days`` (default 90) after pinning, with expiry observable
    via the stored ``retention_expires_at`` (see :func:`artifact_expired`). The bundle itself is
    left UNTOUCHED by redaction (breaking reproduction is worse than a leaked secret in evidence
    prose); only ``diff_text``/``evidence_text`` are scanned. Returns the write ack including the
    new artifact's id."""
    if not commit_sha:
        raise ValueError("commit_sha is required")
    bundle_bytes = build_repro_bundle(repo_path, commit_sha)
    pinned_at = time.time()
    meta: dict[str, Any] = {
        "project": project,
        "ticket_id": ticket_id,
        "commit_sha": commit_sha,
        "bundle_b64": base64.b64encode(bundle_bytes).decode("ascii"),
        "diff": redact_secrets(diff_text),
        "evidence": redact_secrets(evidence_text),
        "pinned_at": pinned_at,
        "while_gating": bool(while_gating),
        "retention_days": int(retention_days),
        "retention_expires_at": pinned_at + int(retention_days) * 86400,
    }
    _praxis.ensure_space(_praxis.FACTORY_LEARNINGS_SPACE, name="factory-learnings")
    return _praxis._request(
        "POST", "/insights",
        body={"insight": f"pinned artifact for {project}/{ticket_id} @ {commit_sha}",
              "category": ARTIFACT_CATEGORY, "source": source, "meta": meta},
        space=_praxis.FACTORY_LEARNINGS_SPACE, snapshot=_praxis.FACTORY_ARTIFACTS_SNAPSHOT,
    )


def read_artifact(artifact_id: str) -> dict[str, Any]:
    """Read one pinned artifact back (GET) — the path proof/re-proof execution reads the bundle
    and evidence from (R7); read-only, same sole-writer guarantee as :func:`read_lessons`."""
    return _praxis.get_fact(artifact_id, space=_praxis.FACTORY_LEARNINGS_SPACE,
                            snapshot=_praxis.FACTORY_ARTIFACTS_SNAPSHOT)


def artifact_expired(meta: dict[str, Any], *, now: float | None = None) -> bool:
    """The default retention policy (R7/KD7), as a pure function of a pinned artifact's stored
    meta: never expired while its check is gating; otherwise expired once ``now`` passes the
    stored ``retention_expires_at`` — observable without any lifecycle sweep running first."""
    if bool(meta.get("while_gating")):
        return False
    expires_at = meta.get("retention_expires_at")
    if expires_at is None:
        return False
    return (now if now is not None else time.time()) > float(expires_at)


def decode_bundle(meta: dict[str, Any]) -> bytes:
    """Decode a pinned artifact's stored bundle back to raw bytes for :func:`materialize_bundle`."""
    return base64.b64decode(str(meta.get("bundle_b64") or ""))


# --------------------------------------------------------------------------- FL5: the fail-then-pass proof engine (R6/R7/E4/E5/E6/D1/D4)
# attempt_proof (above) is the FL2 placeholder single-shot shape kept for back-compat; this is the
# real machine-strict proof R6 describes, always run in a DISPOSABLE ISOLATED WORKTREE — never the
# live project checkout, whose HEAD must not move (concurrent sessions share it).

def _default_worktree_runner(run: str, cwd: Path) -> bool:  # pragma: no cover - real subprocess
    return subprocess.run(run, shell=True, cwd=str(cwd), check=False).returncode == 0


def run_fail_then_pass_proof(run: str, *, bad_artifact_meta: dict[str, Any],
                             healthy_repo_path: str | Path, healthy_ref: str = "HEAD",
                             repeat_count: int = DEFAULT_REPEAT_COUNT,
                             executor: Callable[[str, Path], bool] | None = None) -> dict[str, Any]:
    """R6/R7 — the fail-then-pass proof engine for ONE candidate ``run`` body. Materializes the
    pinned bad artifact (:func:`decode_bundle` + :func:`materialize_bundle`) and the designated
    healthy reference (:func:`build_repro_bundle` + :func:`materialize_bundle`) EACH into their own
    disposable directory — never the live project checkout, whose HEAD/refs are never touched
    (``build_repro_bundle``'s throwaway-ref dance and a plain ``git clone`` off a bundle file are
    both read-only w.r.t. the source repo). ``run`` executes ``repeat_count`` times per side (D4) so
    a nondeterministic result yields no proof rather than a coin-flip verdict (E6).

    Returns ``{"status", "reason", "flag", ...}``; NEVER raises — an irreproducible pin (E5) is a
    ``report_only`` verdict with ``flag=True``, not an exception:

    - ``status="proven"`` — FAILED every repeat on the bad artifact AND PASSED every repeat on the
      healthy reference (R6): the only verdict eligible to gate.
    - ``status="report_only", reason="vacuous-pass-on-bad-artifact"`` — the check does not even
      reproduce the failure it was drafted from (E4); the caller should redraft.
    - ``status="report_only", reason="fails-both"`` — a non-discriminative fail-only check; still
      inserts (report_only, per R6) rather than being discarded.
    - ``status="report_only", reason="pin-irreproducible"|"healthy-reference-irreproducible", flag=True``
      — the pinned bundle (or the healthy ref) could not be re-materialized (E5): no proof, flagged.
    - ``status="unproven", reason="flaky-bad-artifact"|"flaky-healthy-reference"`` — a repeat
      disagreed with a prior repeat on the SAME side (E6): flaky proof = no proof.
    """
    do_run = executor or _default_worktree_runner
    reps = max(1, int(repeat_count))

    try:
        with tempfile.TemporaryDirectory() as bad_root:
            bad_clone = materialize_bundle(decode_bundle(bad_artifact_meta), bad_root)
            bad_results = [do_run(run, bad_clone) for _ in range(reps)]
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        return {"status": STATE_REPORT_ONLY, "reason": "pin-irreproducible", "flag": True,
                "detail": str(exc)}

    if len(set(bad_results)) != 1:
        return {"status": "unproven", "reason": "flaky-bad-artifact", "flag": False}
    bad_failed = not bad_results[0]

    try:
        healthy_bundle = build_repro_bundle(healthy_repo_path, healthy_ref)
        with tempfile.TemporaryDirectory() as good_root:
            good_clone = materialize_bundle(healthy_bundle, good_root)
            good_results = [do_run(run, good_clone) for _ in range(reps)]
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        return {"status": STATE_REPORT_ONLY, "reason": "healthy-reference-irreproducible",
                "flag": True, "detail": str(exc)}

    if len(set(good_results)) != 1:
        return {"status": "unproven", "reason": "flaky-healthy-reference", "flag": False}
    good_passed = good_results[0]

    if bad_failed and good_passed:
        return {"status": "proven", "reason": None, "flag": False}
    if not bad_failed:
        return {"status": STATE_REPORT_ONLY, "reason": "vacuous-pass-on-bad-artifact", "flag": False}
    return {"status": STATE_REPORT_ONLY, "reason": "fails-both", "flag": False}


def attempt_fail_then_pass_proof(run_candidates: list[str], *, bad_artifact_meta: dict[str, Any],
                                 healthy_repo_path: str | Path, healthy_ref: str = "HEAD",
                                 repeat_count: int = DEFAULT_REPEAT_COUNT,
                                 redraft_budget: int = DEFAULT_REDRAFT_BUDGET,
                                 executor: Callable[[str, Path], bool] | None = None) -> dict[str, Any]:
    """R6/D1 — the bounded-redraft wrapper around :func:`run_fail_then_pass_proof`. Tries each of
    ``run_candidates`` (the drafted run, then successive redrafts) up to ``redraft_budget``
    attempts, stopping at the FIRST verdict that is not a vacuous pass-on-bad-artifact — only E4's
    vacuous case calls for a redraft; every other verdict (proven, fails-both, flaky, irreproducible
    pin) is final and inserts (or is flagged) immediately. Exhausting the budget on nothing but
    vacuous passes yields ``status="check-undraftable"`` (flagged): no gating check is inserted and
    the lesson lands alone (R6)."""
    candidates = [c for c in (run_candidates or []) if c]
    if not candidates:
        raise ValueError("run_candidates is required")
    budget = max(1, int(redraft_budget))
    attempts = 0
    last_run = candidates[0]
    for run in candidates[:budget]:
        attempts += 1
        last_run = run
        result = run_fail_then_pass_proof(
            run, bad_artifact_meta=bad_artifact_meta, healthy_repo_path=healthy_repo_path,
            healthy_ref=healthy_ref, repeat_count=repeat_count, executor=executor,
        )
        if result["status"] == "proven":
            return {"status": "proven", "run": run, "flag": False, "reason": None, "attempts": attempts}
        if result["reason"] == "vacuous-pass-on-bad-artifact":
            continue  # E4: redraft and try again
        return {"status": result["status"], "run": run, "flag": bool(result.get("flag", False)),
                "reason": result["reason"], "attempts": attempts}
    return {"status": PROOF_CHECK_UNDRAFTABLE, "run": last_run, "flag": True,
            "reason": "vacuous-after-redraft-budget", "attempts": attempts}


def _cmd_ingest(args: argparse.Namespace) -> int:
    result = write_lesson(args.text, source=args.source)
    print(result.get("summary") or result.get("id") or "ok")
    return 0


def _cmd_read(args: argparse.Namespace) -> int:
    for hit in read_lessons(args.query, top_k=args.top_k):
        text = str(hit.get("text") or "")
        print(f"{hit.get('id', '')}\t{text}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="agent_factory.ingestion_api",
        description="The sole writer of the shared factory-learnings space (FL1) and of every "
                    "project's building/planning validation checks (FL2). This CLI shell covers the "
                    "lesson read/write primitives only; the full ingest/widen/suspend/kill-switch/"
                    "regress/reclassify sequence (R1) is the agent_factory.ingestion_api Python API.")
    sub = ap.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="write a lesson into the factory-learnings space")
    ingest.add_argument("text", help="the lesson text")
    ingest.add_argument("--source", default=None, help="provenance pointer for the lesson")
    ingest.set_defaults(func=_cmd_ingest)

    read = sub.add_parser("read", help="read lessons from the factory-learnings space (read-only)")
    read.add_argument("query", nargs="?", default="", help="similarity query; omit for all lessons")
    read.add_argument("--top-k", type=int, default=10, dest="top_k")
    read.set_defaults(func=_cmd_read)

    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
