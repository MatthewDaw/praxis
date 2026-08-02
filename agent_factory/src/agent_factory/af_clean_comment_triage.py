"""B10/idea 12 — af-clean's per-comment triage by information gain, never by density.

A comment is deletable only if its content words are a near-subset of the identifier tokens it
annotates (it restates the signature it precedes). A comment introducing tokens absent from that
context is presumed to carry WHY -- a reason, a non-obvious invariant, a cost, or a rejected
alternative -- and is protected. Ambiguous comments (neither a clean restatement nor a clear
rationale) survive by default: keep-by-default protects a comment's existence, never its accuracy.

Structural dividers are protected outright, ahead of any overlap math: a banner's label names the
section beneath it, which makes it score as a perfect restatement when it is nothing of the kind.

This module never deletes anything itself -- it only classifies. The caller (whatever proposes a
comment-removal diff) is expected to only ever act on an ``eligible`` verdict.
"""

from __future__ import annotations

import re
from typing import Iterable, NamedTuple

# Explicit WHY markers (B10/idea 12: reason, invariant, cost, or rejected alternative). A match
# here always protects the comment regardless of how much it overlaps the identifier tokens --
# these are the exact kinds of information the 10-100x missing-comment-cost asymmetry protects.
_WHY_MARKERS = re.compile(
    r"\b(because|since|invariant|precondition|postcondition|requires|ensures|must|never|always"
    r"|instead\s+of|rather\s+than|rejected|avoid|workaround|due\s+to|otherwise|cost)\b"
    r"|\bo\([^)]*\)",
    re.IGNORECASE,
)

# A comment restating its signature only if effectively ALL of its content words are covered by
# the identifier tokens ("near-subset"); a comment that is mostly novel is presumed to carry WHY.
_NEAR_SUBSET_OVERLAP = 0.85
_NOVEL_PRESUMED_WHY = 0.5

_STOPWORDS = frozenset({
    "a", "an", "the", "this", "that", "these", "those", "is", "are", "was", "were", "be", "been",
    "it", "its", "of", "to", "for", "in", "on", "at", "and", "or", "here", "we", "our",
})

_WORD_RE = re.compile(r"[a-zA-Z0-9]+")

# A structural DIVIDER: a run of repeated rule characters, as in
#   # --- orgs --------------------------------------------------------------
#   # =========================================================================
# Token-overlap says nothing useful about these. The label in a banner is SUPPOSED to name the
# section beneath it, so it scores a perfect overlap and reads as a pure restatement -- five of
# these were proposed for deletion on a real repo. A divider is layout, not a claim about the code,
# and removing it silently reflows a file's structure.
_DIVIDER = re.compile(r"([-=*#~_+])\1{3,}")


class CommentFinding(NamedTuple):
    """The triage verdict for one comment.

    ``verdict`` is one of ``"eligible"`` (restates the signature -- a removal candidate),
    ``"protected"`` (carries WHY -- never a removal candidate), or ``"ambiguous"`` (neither --
    survives by default). ``reason`` is human-readable.
    """

    comment: str
    verdict: str
    reason: str


def _words(text: str) -> frozenset[str]:
    """Lowercased content words: split words, drop stopwords, snake/camel-split identifiers."""
    raw = (m.group(0) for m in _WORD_RE.finditer(text))
    out: set[str] = set()
    for tok in raw:
        for piece in re.split(r"(?<=[a-z0-9])(?=[A-Z])|_", tok):
            piece = piece.lower()
            if piece and piece not in _STOPWORDS:
                out.add(piece)
    return frozenset(out)


def signature_tokens(name: str, params: Iterable[str] = ()) -> frozenset[str]:
    """The identifier tokens a comment annotating ``name(params...)`` may restate: the function
    name and its parameter names, each split on snake/camel case boundaries."""
    tokens: set[str] = set()
    for identifier in (name, *params):
        tokens |= _words(identifier)
    return frozenset(tokens)


def classify_comment(text: str, identifier_tokens: Iterable[str]) -> CommentFinding:
    """Triage one comment against the identifier tokens of the code it annotates (B10).

    A WHY marker (reason/invariant/cost/rejected-alternative language) always protects the
    comment. Otherwise: near-total overlap with ``identifier_tokens`` means the comment merely
    restates the signature (eligible for deletion); mostly-novel content is presumed to carry WHY
    (protected); anything in between -- or a comment with no content words at all -- is ambiguous
    and survives by default.
    """
    if _WHY_MARKERS.search(text):
        return CommentFinding(
            text, "protected",
            f"comment states a reason/invariant/cost/rejected-alternative ({text!r}); protected",
        )

    if _DIVIDER.search(text):
        return CommentFinding(
            text, "protected",
            "comment is a structural divider/banner, not a claim about the code it precedes; "
            "its label naming the section below is the POINT, not a restatement",
        )

    content = _words(text)
    if not content:
        return CommentFinding(text, "ambiguous", "comment has no content words; survives by default")

    id_tokens = frozenset(identifier_tokens)
    overlap = len(content & id_tokens) / len(content)

    if overlap >= _NEAR_SUBSET_OVERLAP:
        return CommentFinding(
            text, "eligible",
            f"comment content is a near-subset of the identifier tokens it annotates "
            f"(overlap={overlap:.2f}); restates the signature",
        )
    if overlap < _NOVEL_PRESUMED_WHY:
        return CommentFinding(
            text, "protected",
            f"comment introduces tokens absent from the annotated identifiers "
            f"(overlap={overlap:.2f}); presumed to carry WHY",
        )
    return CommentFinding(
        text, "ambiguous",
        f"comment partially overlaps the identifier tokens (overlap={overlap:.2f}); "
        "neither a clean restatement nor a clear rationale; survives by default",
    )
