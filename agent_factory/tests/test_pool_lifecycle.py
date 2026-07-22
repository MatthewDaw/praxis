"""U6 — orphan pruning keeps the candidate pool bounded (`pool_lifecycle.orphaned_candidate_ids`)."""

import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from agent_factory.pool_lifecycle import orphaned_candidate_ids  # noqa: E402


def cand(cid, applies_to):
    return {"id": cid, "meta": {"check_id": cid, "candidate": True, "applies_to": applies_to}}


def test_candidate_matching_a_live_ticket_is_kept():
    cands = [cand("keep", ["auth"])]
    assert orphaned_candidate_ids(cands, [{"auth", "backend"}]) == []


def test_candidate_matching_no_live_ticket_is_orphaned():
    cands = [cand("dead", ["billing"])]
    assert orphaned_candidate_ids(cands, [{"auth"}, {"ui"}]) == ["dead"]


def test_wildcard_candidate_is_never_orphaned():
    cands = [cand("universal", ["*"])]
    assert orphaned_candidate_ids(cands, []) == []          # even with no live tickets


def test_empty_applies_to_is_orphaned():
    cands = [cand("nowhere", [])]
    assert orphaned_candidate_ids(cands, [{"auth"}]) == ["nowhere"]


def test_mixed_pool_returns_only_orphans_sorted():
    cands = [cand("a-dead", ["gone"]), cand("keep", ["auth"]), cand("z-dead", ["also-gone"])]
    assert orphaned_candidate_ids(cands, [{"auth"}]) == ["a-dead", "z-dead"]


def test_tag_normalization_matches_casing():
    cands = [cand("keep", ["Auth"])]
    assert orphaned_candidate_ids(cands, [{"auth"}]) == []   # normalized match, not an orphan
