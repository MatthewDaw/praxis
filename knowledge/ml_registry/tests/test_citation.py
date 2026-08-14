"""R7 acceptance: idea citation resolution (basis + three-valued reference resolution).

Every registry idea carries a ``basis`` of ``external``, ``direct`` or ``reasoned``. A
``reference`` is resolved only through a CLOSED allowlist of arXiv ids and DOIs -- any
other URL form lands ``reasoned`` immediately and never calls the resolver (no outbound
fetch). Resolution against an allow-listed reference is three-valued:

* resolves      -> basis stays/lands ``external``, resolution=``resolved``, the resolved
  title and authors are recorded.
* resolves as non-existent -> basis downgrades to ``reasoned`` with a downgrade note,
  resolution=``non-existent``.
* unreachable   -> resolution=``unreachable``; basis is left untouched (neither
  downgraded nor treated as verified); retried on the next ideation pass; only the 3rd
  CONSECUTIVE unreachable attempt downgrades basis to ``reasoned`` (with a downgrade
  note), after which the streak resets.
"""

from __future__ import annotations

import pytest

from knowledge.ml_registry.citation import (
    BASIS_DIRECT,
    BASIS_EXTERNAL,
    BASIS_REASONED,
    RESOLUTION_NON_EXISTENT,
    RESOLUTION_RESOLVED,
    RESOLUTION_UNREACHABLE,
    ResolvedCitation,
    ResolverUnreachable,
    reference_kind,
    resolve_citation,
)


class _CountingResolver:
    """A fake resolver that records every reference it was actually called with."""

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.calls: list[str] = []

    def __call__(self, reference: str) -> ResolvedCitation | None:
        self.calls.append(reference)
        if self.outcome == "unreachable":
            raise ResolverUnreachable(reference)
        if self.outcome == "non-existent":
            return None
        return ResolvedCitation(title="Attention Is All You Need", authors=("Vaswani", "Shazeer"))


@pytest.mark.parametrize(
    "reference,expected",
    [
        ("2301.12345", "arxiv"),
        ("arXiv:2301.12345", "arxiv"),
        ("https://arxiv.org/abs/2301.12345", "arxiv"),
        ("2301.12345v2", "arxiv"),
        ("10.1000/xyz123", "doi"),
        ("doi:10.1000/xyz123", "doi"),
        ("https://doi.org/10.1000/xyz123", "doi"),
        ("https://blog.example.com/post", "other"),
        ("a hunch, no citation", "other"),
    ],
)
def test_reference_kind_classifies_the_closed_allowlist(reference: str, expected: str) -> None:
    assert reference_kind(reference) == expected


def test_a_resolving_reference_lands_external_with_title_and_authors_recorded() -> None:
    resolver = _CountingResolver("resolved")
    patch = resolve_citation("2301.12345", {}, resolver)
    assert patch["basis"] == BASIS_EXTERNAL
    assert patch["resolution"] == RESOLUTION_RESOLVED
    assert patch["title"] == "Attention Is All You Need"
    assert patch["authors"] == ["Vaswani", "Shazeer"]
    assert resolver.calls == ["2301.12345"]


def test_a_non_existent_reference_downgrades_to_reasoned_with_a_downgrade_note() -> None:
    resolver = _CountingResolver("non-existent")
    patch = resolve_citation("10.1000/xyz123", {}, resolver)
    assert patch["basis"] == BASIS_REASONED
    assert patch["resolution"] == RESOLUTION_NON_EXISTENT
    assert "downgrade_note" in patch and patch["downgrade_note"]


def test_an_unreachable_reference_is_neither_downgraded_nor_treated_as_verified() -> None:
    resolver = _CountingResolver("unreachable")
    patch = resolve_citation("2301.12345", {}, resolver)
    assert patch["resolution"] == RESOLUTION_UNREACHABLE
    assert "basis" not in patch
    assert patch["unreachable_streak"] == 1


def test_unreachable_is_retried_and_downgrades_only_on_the_3rd_consecutive_failure() -> None:
    resolver = _CountingResolver("unreachable")
    meta: dict[str, object] = {}

    patch1 = resolve_citation("2301.12345", meta, resolver)
    meta.update(patch1)
    assert "basis" not in patch1
    assert meta["unreachable_streak"] == 1

    patch2 = resolve_citation("2301.12345", meta, resolver)
    meta.update(patch2)
    assert "basis" not in patch2
    assert meta["unreachable_streak"] == 2

    patch3 = resolve_citation("2301.12345", meta, resolver)
    meta.update(patch3)
    assert patch3["basis"] == BASIS_REASONED
    assert "downgrade_note" in patch3 and patch3["downgrade_note"]
    assert meta["unreachable_streak"] == 0
    assert resolver.calls == ["2301.12345"] * 3


def test_reaching_the_reference_after_prior_unreachable_attempts_resets_the_streak() -> None:
    unreachable_resolver = _CountingResolver("unreachable")
    meta: dict[str, object] = {}
    meta.update(resolve_citation("2301.12345", meta, unreachable_resolver))
    assert meta["unreachable_streak"] == 1

    resolved_resolver = _CountingResolver("resolved")
    meta.update(resolve_citation("2301.12345", meta, resolved_resolver))
    assert meta["basis"] == BASIS_EXTERNAL
    assert meta["unreachable_streak"] == 0


def test_a_reference_in_any_other_url_form_lands_reasoned_and_never_calls_the_resolver() -> None:
    resolver = _CountingResolver("resolved")
    patch = resolve_citation("https://blog.example.com/post", {}, resolver)
    assert patch["basis"] == BASIS_REASONED
    assert patch["resolution"] is None
    assert resolver.calls == []  # no outbound fetch attempted


def test_a_blank_reference_lands_direct_and_never_calls_the_resolver() -> None:
    resolver = _CountingResolver("resolved")
    patch = resolve_citation("", {}, resolver)
    assert patch["basis"] == BASIS_DIRECT
    assert patch["resolution"] is None
    assert resolver.calls == []
