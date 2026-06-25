"""U1: offline tests for PR/commit -> PRDocument assembly + diff summarization.

Every test injects a fake fetcher (argv -> stdout) so nothing shells out to `gh`
or `git`. Covers R1 (input assembly, unit_source) and R3 (last-N merged listing).
"""

from __future__ import annotations

import json

import pytest

from knowledge.injestion.pr_source import (
    build_commit_document,
    build_pr_document,
    list_merged_prs,
    summarize_diff,
)


def _fake(responses: dict[tuple, str]):
    """Build a fetcher that returns canned stdout keyed by the argv tuple."""

    def fetch(argv: list[str]) -> str:
        key = tuple(argv)
        if key not in responses:
            raise AssertionError(f"unexpected fetch argv: {argv}")
        return responses[key]

    return fetch


_DIFF_40 = "\n".join(
    ["diff --git a/knowledge/clustering.py b/knowledge/clustering.py",
     "index 111..222 100644",
     "--- a/knowledge/clustering.py",
     "+++ b/knowledge/clustering.py"]
    + [f"+    line_{i}" for i in range(36)]
)


def test_happy_path_assembles_full_document():
    # Covers R1: body, both review comments, files-changed summary, and diff present.
    pr_json = json.dumps({
        "title": "Lower UMAP n_neighbors",
        "body": "Topics were collapsing into one blob; cap n_neighbors low.",
        "reviews": [{"body": "Nice, this fixes the mega-cluster."},
                    {"body": "Add a regression guard?"}],
    })
    fetch = _fake({
        ("gh", "pr", "view", "57", "--json", "title,body,reviews"): pr_json,
        ("gh", "pr", "diff", "57"): _DIFF_40,
    })

    doc = build_pr_document(57, fetch=fetch)
    text = doc.render()

    assert doc.unit_source == "git/pr:57"
    assert "Topics were collapsing into one blob" in text
    assert "Nice, this fixes the mega-cluster." in text
    assert "Add a regression guard?" in text
    assert "knowledge/clustering.py" in text  # files-changed summary
    assert "line_0" in doc.diff  # diff body carried
    assert doc.truncated is False


def test_unit_source_for_commit_input():
    # Covers R1: commit-fallback carries git/commit:<sha>, subject -> title, body -> body.
    sha = "d892e88"
    fetch = _fake({
        ("git", "show", "-s", "--format=%s%n%x00%n%b", sha):
            "fix(clustering): lower UMAP n_neighbors\n\x00\nKeep n_neighbors low so topics segment.",
        ("git", "show", sha, "--format=", "--no-color"): _DIFF_40,
    })

    doc = build_commit_document(sha, fetch=fetch)

    assert doc.unit_source == "git/commit:d892e88"
    assert doc.title == "fix(clustering): lower UMAP n_neighbors"
    assert "Keep n_neighbors low" in doc.body


def test_diff_cap_prefers_non_test_hunks_and_flags_truncation():
    # Covers the ~300-line cap: a 1,000-line diff truncates, non-test hunks first.
    src_hunk = "\n".join(
        ["diff --git a/knowledge/core.py b/knowledge/core.py"]
        + [f"+    core_{i}" for i in range(600)]
    )
    test_hunk = "\n".join(
        ["diff --git a/tests/test_core.py b/tests/test_core.py"]
        + [f"+    test_{i}" for i in range(600)]
    )
    summary, capped, truncated = summarize_diff(src_hunk + "\n" + test_hunk, cap=300)

    lines = capped.splitlines()
    assert len(lines) <= 300
    assert truncated is True
    assert "core_0" in capped  # the signal hunk leads
    assert "test_0" not in capped  # the test hunk was de-prioritized off the end
    assert "knowledge/core.py" in summary and "tests/test_core.py" in summary  # all files listed


def test_pr_with_no_reviews_assembles_cleanly():
    # Edge: no review threads -> no empty "REVIEW COMMENTS:" header noise.
    pr_json = json.dumps({"title": "T", "body": "B", "reviews": []})
    fetch = _fake({
        ("gh", "pr", "view", "10", "--json", "title,body,reviews"): pr_json,
        ("gh", "pr", "diff", "10"): _DIFF_40,
    })

    doc = build_pr_document(10, fetch=fetch)

    assert doc.reviews == []
    assert "REVIEW COMMENTS" not in doc.render()


def test_list_merged_excludes_open_and_draft():
    # Covers R3: an open PR in the fixture is excluded; capped at the limit.
    listing = json.dumps([
        {"number": 57, "state": "MERGED"},
        {"number": 56, "state": "MERGED"},
        {"number": 55, "state": "OPEN"},
    ])
    fetch = _fake({
        ("gh", "pr", "list", "--state", "merged", "--limit", "3",
         "--json", "number,state"): listing,
    })

    assert list_merged_prs(3, fetch=fetch) == [57, 56]


def test_fetcher_error_raises_not_silent_empty():
    # Error: a fetcher that raises surfaces the error, not a silent empty document.
    def boom(argv: list[str]) -> str:
        raise RuntimeError("gh exited 1: not authenticated")

    with pytest.raises(RuntimeError, match="not authenticated"):
        build_pr_document(1, fetch=boom)
