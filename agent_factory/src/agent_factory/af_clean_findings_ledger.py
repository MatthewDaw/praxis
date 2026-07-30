"""R41 — the af-clean findings ledger: a closure-aware cleared-file skip cache, plus a
sticky, symbol-scoped rejection record.

Two independent record families live in this ledger, each keyed on a different scope so a
change in one part of a file cannot silently invalidate an unrelated decision elsewhere in
that same file:

- **Cleared-file skip cache** (``record_cleared_file`` / ``should_skip_judgment``) — keyed
  on ``(file_path, rubric_version)`` and matched against the file's own content hash PLUS
  its transitive dependency closure hash and the job inventory hash. A hit means "skip the
  LLM judge pass for this file entirely" — but this is the ONLY thing the cache short-
  circuits. Reachability and same-job (cross-finding) matching are always recomputed
  repo-wide on every run; they are never read from this cache, so a file whose caller was
  deleted elsewhere in the repo (changing the closure hash, even though the file's own
  text is untouched) is still re-evaluated rather than silently skipped.

- **Per-symbol rejection record** (``record_rejection`` / ``is_rejected``) — keyed on the
  symbol id and a hash of the *symbol's own normalized source text only*, never the whole
  file. An unrelated edit elsewhere in the file, or a later excision round that rewrites
  the file around it, leaves the rejection's key unchanged, so a declined or quarantined
  finding does not re-surface. Only a change to the symbol's own text re-opens it for
  re-adjudication. The rubric version at rejection time is recorded for reporting, but is
  deliberately NOT part of the expiry key: bumping the rubric version must not mass-expire
  every prior rejection in one shot (that would force the judge to re-relitigate the whole
  repo's history on every rubric change). Re-adjudicating a batch under a new rubric is a
  separate, explicit operation this module does not perform implicitly.

A **reachability veto** (``record_reachability_veto``) — a decision to treat an apparently
unreachable symbol as reachable anyway (e.g. reflection, DI registry, dynamic dispatch) —
always requires a named reason; an anonymous veto is refused outright, because a veto with
no stated justification is unauditable and cannot be reviewed later.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional


def normalize_symbol_text(source: str) -> str:
    """Collapse incidental whitespace so line-shift-only churn doesn't change the hash.

    Blank lines and leading/trailing whitespace on each line are stripped before hashing,
    so reformatting (or an excision round shifting the symbol a few lines up or down)
    never itself expires a rejection tied to this symbol's own text.
    """
    lines = [line.strip() for line in source.strip().splitlines()]
    return "\n".join(line for line in lines if line)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def symbol_text_hash(source: str) -> str:
    """Content hash of a symbol's own normalized source (the rejection key material)."""
    return _sha256(normalize_symbol_text(source))


class ReachabilityVetoRefused(ValueError):
    """Raised when a reachability veto is recorded without a named reason."""


@dataclass(frozen=True)
class ClearedFileEntry:
    """One cleared-file skip-cache record."""

    file_path: str
    content_hash: str
    rubric_version: str
    closure_hash: str
    job_inventory_hash: str


@dataclass(frozen=True)
class RejectionEntry:
    """One per-symbol rejection record."""

    symbol_id: str
    symbol_text_hash: str
    rubric_version: str
    review_state: str  # e.g. "declined" | "quarantined"


class FindingsLedger:
    """In-memory findings ledger for af-clean.

    A persistence layer (Praxis-backed, or the R40 degraded on-disk fallback store) can
    load/dump this ledger's records; this class holds only the keying and expiry rules
    that must be true regardless of where the records are stored.
    """

    def __init__(self) -> None:
        self._cleared: dict[tuple[str, str], ClearedFileEntry] = {}
        self._rejections: dict[str, RejectionEntry] = {}
        self._reachability_vetoes: dict[str, str] = {}

    # ---- cleared-file skip cache --------------------------------------------------

    def record_cleared_file(
        self,
        *,
        file_path: str,
        content_hash: str,
        rubric_version: str,
        closure_hash: str,
        job_inventory_hash: str,
    ) -> None:
        key = (file_path, rubric_version)
        self._cleared[key] = ClearedFileEntry(
            file_path=file_path,
            content_hash=content_hash,
            rubric_version=rubric_version,
            closure_hash=closure_hash,
            job_inventory_hash=job_inventory_hash,
        )

    def should_skip_judgment(
        self,
        *,
        file_path: str,
        content_hash: str,
        rubric_version: str,
        closure_hash: str,
        job_inventory_hash: str,
    ) -> bool:
        """True only when the file's own content, its transitive dependency closure, and
        the job inventory are ALL unchanged since the file was last cleared. Reachability
        and same-job matching are not represented here at all — callers must always
        recompute those fresh, regardless of this result.
        """
        entry = self._cleared.get((file_path, rubric_version))
        if entry is None:
            return False
        return (
            entry.content_hash == content_hash
            and entry.closure_hash == closure_hash
            and entry.job_inventory_hash == job_inventory_hash
        )

    # ---- per-symbol rejection -------------------------------------------------------

    def record_rejection(
        self,
        *,
        symbol_id: str,
        symbol_source: str,
        rubric_version: str,
        review_state: str = "declined",
    ) -> None:
        self._rejections[symbol_id] = RejectionEntry(
            symbol_id=symbol_id,
            symbol_text_hash=symbol_text_hash(symbol_source),
            rubric_version=rubric_version,
            review_state=review_state,
        )

    def is_rejected(
        self,
        *,
        symbol_id: str,
        symbol_source: str,
        current_rubric_version: Optional[str] = None,
    ) -> bool:
        """True iff a rejection is on file for ``symbol_id`` AND the symbol's own text is
        unchanged since. ``current_rubric_version`` is accepted (and deliberately ignored
        in the expiry decision) so a rubric-version bump alone can never expire a batch of
        prior rejections — only the symbol's own text changing can.
        """
        entry = self._rejections.get(symbol_id)
        if entry is None:
            return False
        return entry.symbol_text_hash == symbol_text_hash(symbol_source)

    def rejection_state(self, symbol_id: str) -> Optional[RejectionEntry]:
        """The recorded rejection entry (with its review state), or ``None`` if absent."""
        return self._rejections.get(symbol_id)

    # ---- reachability veto -----------------------------------------------------------

    def record_reachability_veto(self, *, symbol_id: str, reason: Optional[str]) -> None:
        """Record a decision to treat ``symbol_id`` as reachable despite static analysis
        finding no reachable path to it. Refuses an anonymous veto: ``reason`` must be a
        non-empty, non-whitespace string naming why.
        """
        if not reason or not reason.strip():
            raise ReachabilityVetoRefused(
                f"reachability veto for {symbol_id!r} requires a named reason"
            )
        self._reachability_vetoes[symbol_id] = reason.strip()

    def reachability_veto_reason(self, symbol_id: str) -> Optional[str]:
        """The named reason for a prior reachability veto, or ``None`` if none recorded."""
        return self._reachability_vetoes.get(symbol_id)
