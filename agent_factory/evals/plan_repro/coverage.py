"""The coverage engine — judges a target against a set of parts that must each be covered.

See ``docs/coverage-spine/05-coverage-engine.md``. The model is:

    for each PART (a thing that must be covered)
        -> thoroughly query the candidates related to that part
        -> evaluate coverage (evidence-required)
        -> targeted adversarial confirm on derived / critical / low-confidence claims

This module is the **deterministic orchestration**: the per-part sweep, aggregation, the
no-holes report, and the targeted-adversarial control flow. The two pieces that need
judgment — the *related query* (retrieval) and the *item evaluator* (semantic match) — are
**injected callables**, so the engine is:

- unit-testable with deterministic stand-ins (the lexical baselines below), and
- LLM-backed in real runs (an agent/Workflow supplies an evaluator that calls a model, using
  :func:`build_judge_prompt` / :func:`build_refuter_prompt` for a consistent contract).

The engine is a pure reducer over an ordered part list — deterministic given its injected
callables. Parallel fan-out across parts is an orchestration concern for the caller (the
per-part evaluator is independent), kept out of the engine so tests stay deterministic.

First instantiation: the plan-reproduction eval — parts = the golden features that must have
no holes, candidates = the reproduced plan's features, evaluator = evidence-required match.
The same engine later serves validation (parts = code units, candidates = Praxis checks).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# --- statuses ------------------------------------------------------------------

COVERED = "covered"   # a candidate clearly covers the part
VARIANT = "variant"   # covered, but via materially different wording/scope worth noting
MISSING = "missing"   # no candidate covers the part -> a HOLE
STATUSES = (COVERED, VARIANT, MISSING)

#: The only status that counts as a hole (a coverage failure).
_HOLE = MISSING


# --- data types ----------------------------------------------------------------


@dataclass(frozen=True)
class Feature:
    """One feature/criterion — used for both golden parts and candidate plan items.

    ``derived`` (golden only) marks a feature the raw source never stated (the eval's
    teeth); ``severity`` feeds the targeted-adversarial selection. ``meta`` carries the
    rest (scope, epic, req_id, why_derived, ...).
    """

    id: str
    text: str
    derived: bool = False
    severity: str = "med"
    meta: dict = field(default_factory=dict)


@dataclass
class PartResult:
    """The coverage verdict for one part."""

    part_id: str
    status: str                       # COVERED | VARIANT | MISSING
    derived: bool = False
    evidence: str = ""                # the quoted candidate text that covers it (req. for covered/variant)
    matched_ids: list[str] = field(default_factory=list)
    confidence: float = 1.0
    adversarial: dict | None = None   # {"ran": bool, "refuted": bool, "reason": str} when the refuter ran
    notes: str = ""

    @property
    def is_hole(self) -> bool:
        return self.status == _HOLE


@dataclass
class CoverageReport:
    """Aggregate result over all parts."""

    results: list[PartResult]

    @property
    def holes(self) -> list[PartResult]:
        return [r for r in self.results if r.is_hole]

    @property
    def derived_holes(self) -> list[PartResult]:
        """Holes on derived features — the headline failure (a naive planner misses these)."""
        return [r for r in self.holes if r.derived]

    @property
    def passed(self) -> bool:
        """PASS = zero holes. (Derived holes are highlighted but every hole fails the bar.)"""
        return not self.holes

    def counts(self) -> dict[str, int]:
        c = {s: 0 for s in STATUSES}
        for r in self.results:
            c[r.status] = c.get(r.status, 0) + 1
        return c

    def format(self) -> str:
        c = self.counts()
        n = len(self.results)
        lines = [
            f"coverage: {c[COVERED]} covered, {c[VARIANT]} variant, {c[MISSING]} missing "
            f"(of {n}) -> {'PASS' if self.passed else 'FAIL'}",
        ]
        if self.derived_holes:
            lines.append(f"  DERIVED HOLES ({len(self.derived_holes)}) — the dangerous misses:")
            for r in self.derived_holes:
                lines.append(f"    - {r.part_id}: {r.notes or 'no covering candidate found'}")
        other = [r for r in self.holes if not r.derived]
        if other:
            lines.append(f"  holes ({len(other)}):")
            for r in other:
                lines.append(f"    - {r.part_id}")
        return "\n".join(lines)


# --- injected-callable contracts ----------------------------------------------

#: Retrieve the candidates related to ``part`` (thorough/exhaustive-for-this-part, adaptive
#: count). Returns the neighborhood the evaluator will judge against.
RelatedQuery = Callable[[Feature, "list[Feature]"], "list[Feature]"]

#: Judge whether ``part`` is covered by its ``related`` candidates. Must be evidence-required
#: (quote the covering candidate; default MISSING when uncertain).
ItemEvaluator = Callable[[Feature, "list[Feature]"], PartResult]

#: Adversarial refuter: given a claimed-covered result, return True if the coverage should be
#: REJECTED (i.e. the part is NOT actually covered). Default-true on doubt.
Refuter = Callable[[Feature, PartResult, "list[Feature]"], bool]

#: Decide whether a claimed-covered result earns the (more expensive) adversarial pass.
AdversarialSelect = Callable[[Feature, PartResult], bool]

_CRITICAL_SEVERITIES = {"high", "critical"}
_LOW_CONFIDENCE = 0.75


def default_adversarial_select(part: Feature, result: PartResult) -> bool:
    """Targeted: refute claimed-covered items that are derived, critical, or low-confidence."""
    return (
        part.derived
        or str(part.severity).lower() in _CRITICAL_SEVERITIES
        or result.confidence < _LOW_CONFIDENCE
    )


# --- the engine ----------------------------------------------------------------


def run_coverage(
    parts: Iterable[Feature],
    candidates: list[Feature],
    related_query: RelatedQuery,
    item_evaluator: ItemEvaluator,
    *,
    refuter: Refuter | None = None,
    adversarial_select: AdversarialSelect = default_adversarial_select,
) -> CoverageReport:
    """Sweep every part; judge each against its related candidates; targeted-adversarial confirm.

    Systematic over parts (none skipped) + thorough per part (``related_query`` decides the
    neighborhood). When ``refuter`` is supplied, a claimed-covered result selected by
    ``adversarial_select`` is challenged; a refuted match is downgraded to MISSING.
    """
    results: list[PartResult] = []
    for part in parts:
        related = related_query(part, candidates)
        result = item_evaluator(part, related)
        result.derived = part.derived  # carry the flag onto the result for reporting
        if (
            refuter is not None
            and result.status in (COVERED, VARIANT)
            and adversarial_select(part, result)
        ):
            refuted = bool(refuter(part, result, related))
            result.adversarial = {"ran": True, "refuted": refuted}
            if refuted:
                result.status = MISSING
                result.evidence = ""
                result.notes = (result.notes + " [adversarial refuter rejected the match]").strip()
        results.append(result)
    return CoverageReport(results=results)


# --- deterministic baselines (cheap pre-filter tier + test stand-ins) ----------

_WORD_RE = re.compile(r"[a-z0-9]+")
# Common words that shouldn't drive a match; small + domain-agnostic on purpose.
_STOP = frozenset(
    "a an the and or of to for in on at by with is are be can not no this that "
    "as it its their they each per via from when given so any all only".split()
)


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOP and len(w) > 2}


def _overlap(a: set[str], b: set[str]) -> float:
    """Overlap of ``a``'s tokens found in ``b`` (recall of the part's terms). 0..1."""
    if not a:
        return 0.0
    return len(a & b) / len(a)


def all_related_query(part: Feature, candidates: list[Feature]) -> list[Feature]:
    """Return every candidate — fine when the candidate set is small (the eval's ~78)."""
    return list(candidates)


def lexical_related_query(
    part: Feature,
    candidates: list[Feature],
    *,
    max_k: int = 8,
    ratio: float = 0.5,
    min_score: float = 0.15,
) -> list[Feature]:
    """Adaptive per-part retrieval: rank candidates by token overlap, keep the top band.

    Keeps candidates scoring at least ``ratio`` of the top score (and >= ``min_score``),
    capped at ``max_k`` — an adaptive count (a sharp part returns one, a broad part several).
    Deterministic stand-in for the real semantic ``related-to(part)`` retrieval.
    """
    pt = _tokens(part.text)
    scored = sorted(
        ((_overlap(pt, _tokens(c.text)), c) for c in candidates),
        key=lambda sc: sc[0],
        reverse=True,
    )
    scored = [(s, c) for s, c in scored if s >= min_score]
    if not scored:
        return []
    top = scored[0][0]
    cutoff = max(min_score, top * ratio)
    return [c for s, c in scored[:max_k] if s >= cutoff]


def lexical_evaluator(
    part: Feature,
    related: list[Feature],
    *,
    cover_threshold: float = 0.6,
    variant_threshold: float = 0.4,
) -> PartResult:
    """Deterministic baseline judge by token overlap (the cheap pre-filter tier).

    NOT a substitute for the semantic/LLM judge — lexical match cannot read intent, so it
    will both miss paraphrases and over-credit keyword overlap. Its jobs are: (1) make the
    harness runnable + testable end to end without a model, and (2) serve as the cheap first
    pass that only escalates ambiguous parts to the LLM judge.
    """
    if not related:
        return PartResult(part_id=part.id, status=MISSING, confidence=0.9,
                          notes="no related candidate")
    pt = _tokens(part.text)
    best_score, best = max(((_overlap(pt, _tokens(c.text)), c) for c in related),
                           key=lambda sc: sc[0])
    if best_score >= cover_threshold:
        return PartResult(part_id=part.id, status=COVERED, evidence=best.text,
                          matched_ids=[best.id], confidence=min(1.0, best_score))
    if best_score >= variant_threshold:
        return PartResult(part_id=part.id, status=VARIANT, evidence=best.text,
                          matched_ids=[best.id], confidence=best_score,
                          notes="lexical near-match (confirm semantically)")
    return PartResult(part_id=part.id, status=MISSING, confidence=1.0 - best_score,
                      notes=f"best lexical overlap {best_score:.2f} below threshold")


# --- LLM judge contract (prompt builders + response parser) --------------------


def build_judge_prompt(part: Feature, related: list[Feature]) -> str:
    """The evidence-required coverage prompt an LLM-backed evaluator should use."""
    cand = "\n".join(f"  - [{c.id}] {c.text}" for c in related) or "  (none)"
    return (
        "Decide whether a REQUIRED feature is covered by a candidate plan.\n\n"
        f"REQUIRED feature [{part.id}]: {part.text}\n\n"
        f"Candidate plan features that might cover it:\n{cand}\n\n"
        "Rules:\n"
        "- COVERED: a candidate clearly implements this feature, even if worded differently.\n"
        "- VARIANT: covered, but the candidate's scope/wording differs in a way worth noting.\n"
        "- MISSING: no candidate covers it.\n"
        "- You MUST quote the exact candidate text that covers it. If you cannot quote a "
        "specific covering candidate, the answer is MISSING. Default to MISSING when uncertain.\n\n"
        'Respond with JSON only: {"status":"covered|variant|missing","evidence":'
        '"<quoted candidate text>","matched_ids":["..."],"confidence":0.0-1.0,"notes":"..."}'
    )


def build_refuter_prompt(part: Feature, result: PartResult, related: list[Feature]) -> str:
    """The adversarial prompt: try to REFUTE a claimed-covered match."""
    return (
        "A judge claimed a REQUIRED feature is covered. Argue whether that is WRONG.\n\n"
        f"REQUIRED feature [{part.id}]: {part.text}\n"
        f"Claimed covering candidate: {result.evidence!r}\n\n"
        "Refute the match if the candidate only partially covers it, addresses a different "
        "case, or the overlap is superficial. Default to refuted=true unless the coverage is "
        'clearly complete.\n\n'
        'Respond with JSON only: {"refuted":true|false,"reason":"..."}'
    )


def judge_result_from_response(part: Feature, payload: dict | str) -> PartResult:
    """Parse an LLM judge JSON response into a :class:`PartResult` (tolerant)."""
    data = json.loads(payload) if isinstance(payload, str) else dict(payload)
    status = str(data.get("status", MISSING)).lower()
    if status not in STATUSES:
        status = MISSING
    evidence = str(data.get("evidence", "")).strip()
    # Evidence-required: a covered/variant claim with no quoted evidence is downgraded.
    if status in (COVERED, VARIANT) and not evidence:
        status, notes_extra = MISSING, " [downgraded: no evidence quoted]"
    else:
        notes_extra = ""
    matched = data.get("matched_ids") or []
    try:
        confidence = float(data.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0
    return PartResult(
        part_id=part.id,
        status=status,
        derived=part.derived,
        evidence=evidence,
        matched_ids=[str(m) for m in matched],
        confidence=max(0.0, min(1.0, confidence)),
        notes=(str(data.get("notes", "")).strip() + notes_extra).strip(),
    )


def refuted_from_response(payload: dict | str) -> bool:
    """Parse an LLM refuter JSON response; default to refuted on any parse trouble."""
    try:
        data = json.loads(payload) if isinstance(payload, str) else dict(payload)
        return bool(data.get("refuted", True))
    except Exception:
        return True


# --- loaders -------------------------------------------------------------------


def load_golden(path: str | Path) -> list[Feature]:
    """Load ``golden-features.yaml`` (epics -> features) into a flat list of parts."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out: list[Feature] = []
    for epic in data.get("epics", []):
        epic_name = epic.get("name", "")
        for f in epic.get("features", []):
            out.append(
                Feature(
                    id=str(f["id"]),
                    text=str(f.get("feature", f.get("text", ""))),
                    derived=bool(f.get("derived", False)),
                    severity=str(f.get("severity", "med")),
                    meta={
                        "epic": epic_name,
                        "scope": f.get("scope"),
                        "req_id": f.get("req_id"),
                        "why_derived": f.get("why_derived"),
                    },
                )
            )
    return out


def load_candidate(path: str | Path) -> list[Feature]:
    """Load a candidate plan: a YAML/JSON list of ``{id, text, meta?}`` or ``{features: [...]}``."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    items = data.get("features", []) if isinstance(data, dict) else data
    out: list[Feature] = []
    for i, f in enumerate(items):
        out.append(
            Feature(
                id=str(f.get("id", f"C{i}")),
                text=str(f.get("text", f.get("feature", ""))),
                meta=dict(f.get("meta", {})),
            )
        )
    return out


# --- smoke runner --------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Run the coverage engine (lexical baseline).")
    p.add_argument("golden", help="path to golden-features.yaml")
    p.add_argument("candidate", nargs="?", help="path to a candidate plan; omit to self-cover")
    args = p.parse_args(argv)

    golden = load_golden(args.golden)
    candidates = load_candidate(args.candidate) if args.candidate else [
        Feature(id=g.id, text=g.text) for g in golden  # self-cover sanity: must be 100%
    ]
    report = run_coverage(golden, candidates, lexical_related_query, lexical_evaluator)
    print(report.format())
    return 0 if report.passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
