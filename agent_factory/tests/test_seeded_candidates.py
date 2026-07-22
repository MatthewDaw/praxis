"""U3: the deterministic seeded candidate lane (opt-in, non-gating)."""

from __future__ import annotations

import textwrap

from agent_factory.seeded_checks import load_seeded_checks, seeded_candidates


def _lib(tmp_path):
    p = tmp_path / "seeded_checks.toml"
    p.write_text(textwrap.dedent("""
        [[check]]
        check_id = "universal"
        run = "true"
        applies_to = ["*"]

        [[check]]
        check_id = "auth-only"
        run = "true"
        applies_to = ["auth"]
    """), encoding="utf-8")
    return load_seeded_checks(p)


def test_wildcard_offered_to_every_ticket(tmp_path):
    lib = _lib(tmp_path)
    offered = {c.check_id for c in seeded_candidates([], lib)}
    assert offered == {"universal"}  # tag-less ticket still gets the wildcard candidate


def test_tag_match_offers_scoped_candidate(tmp_path):
    lib = _lib(tmp_path)
    offered = {c.check_id for c in seeded_candidates(["auth"], lib)}
    assert offered == {"universal", "auth-only"}


def test_tag_match_is_case_insensitive(tmp_path):
    lib = _lib(tmp_path)
    offered = {c.check_id for c in seeded_candidates(["Auth"], lib)}
    assert "auth-only" in offered


def test_non_matching_tag_gets_only_wildcard(tmp_path):
    lib = _lib(tmp_path)
    offered = {c.check_id for c in seeded_candidates(["frontend"], lib)}
    assert offered == {"universal"}


def test_default_library_offers_generic_axes_to_any_ticket():
    """The shipped library's TerMinal-derived checks are all wildcard candidates."""
    offered = {c.check_id for c in seeded_candidates([])}
    assert {"correctness-review", "security-review", "error-paths-covered"} <= offered
