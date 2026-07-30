"""R23 — af-clean blames defensive-code constructs before flagging them for removal, and demotes a
finding to advisory (citing the commit) when the introducing commit's message marks it a scar.

Acceptance: a try/except introduced by a commit titled 'fix: handle empty payload' is demoted to
advisory with the commit cited; the same construct introduced by a commit titled 'add feature'
remains eligible for removal.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_factory.af_clean_scar_detection import (
    blame_lines,
    classify_commit_message,
    detect_scar,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _commit_file(repo: Path, name: str, content: str, message: str) -> None:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)


TRY_EXCEPT_SRC = (
    "def handle(payload):\n"
    "    try:\n"
    "        return payload[\"value\"]\n"
    "    except KeyError:\n"
    "        return None\n"
)


def test_acceptance_fix_commit_demotes_to_advisory_citing_commit(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_file(repo, "m.py", TRY_EXCEPT_SRC, "fix: handle empty payload")

    finding = detect_scar(repo, "m.py", 2, 5, construct="try/except")

    assert finding.verdict == "advisory"
    assert finding.commits
    assert "fix: handle empty payload" in finding.reason
    assert finding.commits[0].sha in finding.reason


def test_acceptance_feature_commit_remains_eligible(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_file(repo, "m.py", TRY_EXCEPT_SRC, "add feature")

    finding = detect_scar(repo, "m.py", 2, 5, construct="try/except")

    assert finding.verdict == "eligible"
    assert finding.commits
    assert finding.commits[0].subject == "add feature"


@pytest.mark.parametrize("subject", ["fix: null check", "bug: crash on empty list",
                                     "regression: restore timeout", "hotfix: prod outage",
                                     "incident: mitigate 500s"])
def test_classify_commit_message_matches_each_scar_keyword(subject):
    assert classify_commit_message(subject) is True


@pytest.mark.parametrize("subject", ["fixes #42: guard nulls", "closes issue 17",
                                     "resolves GH-9", "see pull request #3"])
def test_classify_commit_message_matches_issue_or_pr_reference(subject):
    assert classify_commit_message(subject) is True


def test_classify_commit_message_false_for_ordinary_feature_work():
    assert classify_commit_message("add feature") is False
    assert classify_commit_message("refactor: extract helper") is False


def test_classify_commit_message_does_not_false_positive_on_substrings():
    # "prefix"/"debugger" contain "fix"/"bug" as substrings but are not the whole-word keyword.
    assert classify_commit_message("prefix the config keys") is False
    assert classify_commit_message("wire up the debugger UI") is False


def test_blame_lines_returns_the_introducing_commit(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_file(repo, "m.py", TRY_EXCEPT_SRC, "add feature")

    commits = blame_lines(repo, "m.py", 2, 5)

    assert len(commits) == 1
    assert commits[0].subject == "add feature"


def test_blame_lines_finds_the_most_recent_touching_commit_per_line(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_file(repo, "m.py", TRY_EXCEPT_SRC, "add feature")

    fixed = TRY_EXCEPT_SRC.replace("except KeyError:", "except (KeyError, TypeError):")
    _commit_file(repo, "m.py", fixed, "fix: handle empty payload")

    finding = detect_scar(repo, "m.py", 2, 5, construct="try/except")

    # Line 4 (the except clause) was last touched by the fix commit — blame surfaces it, and its
    # presence alone is enough to demote the whole construct to advisory.
    assert finding.verdict == "advisory"
    assert any(c.subject == "fix: handle empty payload" for c in finding.commits)


def test_detect_scar_reason_names_the_construct_and_location(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit_file(repo, "m.py", TRY_EXCEPT_SRC, "add feature")

    finding = detect_scar(repo, "m.py", 2, 5, construct="try/except")

    assert "try/except" in finding.reason
    assert "m.py" in finding.reason
