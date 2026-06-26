"""Offline U1 tests: selection, version filter, two-tier leak screen, difficulty order, manifest.

Runs fully offline — fixture records go straight to ``load_candidates``; the HF
``datasets`` seam (:func:`fetch_rebench_sympy`) is never touched.

    uv run pytest knowledge/evals/swebench/tests/test_instances.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

from knowledge.evals.swebench.instances import (
    Instance,
    gold_files,
    gold_patch_lines,
    load_candidates,
    read_manifest,
    screen_leakage,
    select,
    version_supported,
    write_manifest,
)

FIXTURE = Path(__file__).parent / "fixtures" / "rebench_sample.json"


def _records() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _by_id(instances: list[Instance]) -> dict[str, Instance]:
    return {i.instance_id: i for i in instances}


def _rec(iid, *, version="1.13", created="2025-01-01T00:00:00Z", issue="bug", added_lines=()):
    """A minimal supported record whose gold patch adds ``added_lines`` (for size/leak tests)."""
    body = "".join(f"+{ln}\n" for ln in added_lines)
    patch = f"diff --git a/p.py b/p.py\n--- a/p.py\n+++ b/p.py\n@@ -1 +1 @@\n{body}"
    return {"instance_id": iid, "repo": "sympy/sympy", "version": version,
            "base_commit": "c" * 40, "created_at": created, "problem_statement": issue,
            "patch": patch, "test_patch": "", "FAIL_TO_PASS": [], "PASS_TO_PASS": [],
            "install_config": {}}


def test_version_filter_excludes_unsupported_version():
    chosen = select(load_candidates(_records()), n=10)
    ids = {i.instance_id for i in chosen}
    # 1.9 record (fake-0004-oldver) is outside MAP_REPO_VERSION_TO_SPECS coverage → dropped.
    assert "sympy__sympy-fake-0004-oldver" not in ids
    assert all(version_supported(i.version) for i in chosen)
    assert version_supported("1.13") and not version_supported("1.9")


# ---------------------------------------------------------------------------
# Two-tier leak screen: verbatim (disqualifying) vs symbol (informational).
# ---------------------------------------------------------------------------
def test_screen_flags_verbatim_leak_as_disqualifying():
    leaky = _by_id(load_candidates(_records()))["sympy__sympy-fake-0002-leaky"]
    verbatim, symbol, reason = screen_leakage(leaky)
    assert verbatim is True   # the issue pastes `return radsimp(num / den)`
    assert symbol is False    # verbatim wins; checked first
    assert "gold added line" in reason


def test_screen_marks_symbol_mention_weak_not_disqualifying():
    # Issue NAMES a changed symbol (foo_bar) but pastes no fix line → weak, kept.
    inst = load_candidates([_rec("sympy__sympy-1", issue="foo_bar mishandles empty input",
                                 added_lines=["        return foo_bar(x, default=0)"])])[0]
    verbatim, symbol, reason = screen_leakage(inst)
    assert verbatim is False
    assert symbol is True
    assert "foo_bar" in reason and "weak" in reason


def test_screen_passes_clean_instance():
    clean = _by_id(load_candidates(_records()))["sympy__sympy-fake-0001"]
    verbatim, symbol, reason = screen_leakage(clean)
    assert (verbatim, symbol) == (False, False)
    assert reason == "no problem_statement / gold overlap"


# ---------------------------------------------------------------------------
# select(): exclusion is verbatim-only; verdicts recorded on every instance (R2).
# ---------------------------------------------------------------------------
def test_select_excludes_only_verbatim_leaks_by_default():
    chosen = select(load_candidates(_records()), n=10)
    ids = {i.instance_id for i in chosen}
    # The verbatim-leaked record is dropped; the clean ones remain.
    assert "sympy__sympy-fake-0002-leaky" not in ids
    assert {"sympy__sympy-fake-0001", "sympy__sympy-fake-0003"} <= ids
    # Every chosen instance carries a recorded verdict (R2 — no silent inclusion).
    for inst in chosen:
        assert isinstance(inst.leak_verbatim, bool) and isinstance(inst.leak_symbol, bool)
        assert inst.screen_reason


def test_select_keeps_symbol_only_instances():
    # A symbol-mention instance is NOT excluded (weak tier).
    cands = load_candidates([
        _rec("sympy__sympy-sym", issue="foo_bar is wrong",
             added_lines=["        return foo_bar(x)"]),
    ])
    chosen = select(cands, n=10)
    assert [i.instance_id for i in chosen] == ["sympy__sympy-sym"]
    assert chosen[0].leak_symbol is True and chosen[0].leak_verbatim is False


def test_include_leaked_keeps_verbatim_leaks():
    chosen = select(load_candidates(_records()), n=10, exclude_leaked=False)
    ids = {i.instance_id for i in chosen}
    assert "sympy__sympy-fake-0002-leaky" in ids  # verbatim leak kept when not excluding


# ---------------------------------------------------------------------------
# Ordering: recent (default) vs hard (difficulty).
# ---------------------------------------------------------------------------
def test_recent_order_is_deterministic_desc_by_created_at():
    chosen = select(load_candidates(_records()), n=10)  # default order="recent"
    dates = [i.created_at for i in chosen]
    assert dates == sorted(dates, reverse=True)
    assert chosen[0].instance_id == "sympy__sympy-fake-0003"  # newest supported, clean
    again = select(load_candidates(_records()), n=10)
    assert [i.instance_id for i in again] == [i.instance_id for i in chosen]


def test_hard_order_sorts_by_gold_patch_size():
    # Three clean supported instances; newest is the SMALLEST patch. "hard" must invert
    # the recency order and put the biggest patch first.
    cands = load_candidates([
        _rec("sympy__sympy-small", created="2025-03-01T00:00:00Z", added_lines=["    a = 1"]),
        _rec("sympy__sympy-mid", created="2025-02-01T00:00:00Z",
             added_lines=[f"    x{i} = {i}" for i in range(5)]),
        _rec("sympy__sympy-big", created="2025-01-01T00:00:00Z",
             added_lines=[f"    y{i} = {i}" for i in range(20)]),
    ])
    by_recent = [i.instance_id for i in select(cands, n=3, order="recent")]
    by_hard = [i.instance_id for i in select(cands, n=3, order="hard")]
    assert by_recent == ["sympy__sympy-small", "sympy__sympy-mid", "sympy__sympy-big"]
    assert by_hard == ["sympy__sympy-big", "sympy__sympy-mid", "sympy__sympy-small"]
    assert gold_patch_lines(select(cands, n=1, order="hard")[0]) == 20


def test_since_filters_by_created_date_and_composes_with_order():
    # Same three clean instances spanning Jan–Mar 2025; --since drops the older two, and
    # the cutoff composes with order (the surviving pool still sorts hard = biggest patch).
    cands = load_candidates([
        _rec("sympy__sympy-jan", created="2025-01-15T00:00:00Z", added_lines=["    a = 1"]),
        _rec("sympy__sympy-feb", created="2025-02-10T00:00:00Z",
             added_lines=[f"    x{i} = {i}" for i in range(5)]),
        _rec("sympy__sympy-mar", created="2025-03-20T00:00:00Z",
             added_lines=[f"    y{i} = {i}" for i in range(20)]),
    ])
    since_feb = select(cands, n=10, since="2025-02-01")
    assert {i.instance_id for i in since_feb} == {"sympy__sympy-feb", "sympy__sympy-mar"}
    # Jan instance is gone; pool of 2 still honors hard order (mar's 20-line patch first).
    by_hard = [i.instance_id for i in select(cands, n=10, order="hard", since="2025-02-01")]
    assert by_hard == ["sympy__sympy-mar", "sympy__sympy-feb"]
    # A cutoff after everything yields an empty selection (no crash).
    assert select(cands, n=10, since="2025-12-01") == []


# ---------------------------------------------------------------------------
# Gold-file parse + manifest.
# ---------------------------------------------------------------------------
def test_gold_files_parsed_from_gold_patch():
    by_id = _by_id(load_candidates(_records()))
    assert by_id["sympy__sympy-fake-0001"].gold_files == ["sympy/matrices/dense.py"]
    assert by_id["sympy__sympy-fake-0003"].gold_files == ["sympy/integrals/integrals.py"]
    patch = (
        "diff --git a/pkg/x.py b/pkg/x.py\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/pkg/y.py b/pkg/y.py\n@@ -1 +1 @@\n-c\n+d\n"
    )
    assert gold_files(patch) == ["pkg/x.py", "pkg/y.py"]


def test_manifest_round_trips_to_identical_chosen_set(tmp_path):
    chosen = select(load_candidates(_records()), n=10)
    path = tmp_path / "instances.manifest.json"
    write_manifest(chosen, path)
    rows = read_manifest(path)
    assert rows == [i.to_manifest_row() for i in chosen]
    assert set(rows[0]) == {
        "instance_id", "version", "base_commit", "created_at",
        "gold_files", "leak_verbatim", "leak_symbol", "screen_reason", "human_reviewed",
    }


def test_from_record_carries_install_config_verbatim():
    rec = _records()[0]
    inst = Instance.from_record(rec)
    assert inst.install_config == rec["install_config"]
    assert inst.fail_to_pass == rec["FAIL_TO_PASS"]
    assert inst.pass_to_pass == rec["PASS_TO_PASS"]
    assert inst.human_reviewed is False
    assert (inst.leak_verbatim, inst.leak_symbol) == (False, False)  # unscreened defaults
