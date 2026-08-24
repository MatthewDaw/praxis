"""A ticket is graded only on paths introduced by its provenance-marked commits."""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_factory.ticket_authorship import collect_authorship, verdict_authorship_errors

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          check=True).stdout.strip()


def test_inherited_source_changes_are_not_ticket_authorship(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "base.py").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")

    # This is the 626c70b shape: source inherited through the aligned base, not authored by R3a.
    (repo / "agent_factory.py").write_text("GET YOUR BASE RIGHT\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "tooling change on main")
    (repo / "ticket.py").write_text("R3a work\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feat: baseline reproduction (R3a)")

    got = collect_authorship(repo, base, "HEAD", ["R3a"])
    assert got["R3a"]["paths"] == ["ticket.py"]
    assert "agent_factory.py" not in got["R3a"]["paths"]


def test_a_finding_in_an_unowned_file_is_rejected() -> None:
    authorship = {"R4b": {"commits": ["abc"], "paths": ["baseline.py"]}}
    verdict = {"regressed": [{"id": "R4b", "paths": ["unrelated_test.py"],
                               "reason": "missing annotation"}]}
    assert verdict_authorship_errors(verdict, authorship) == [
        "R4b was blamed for paths it did not author: ['unrelated_test.py']"
    ]


def test_a_located_finding_in_an_authored_file_is_accepted() -> None:
    authorship = {"R3a": {"commits": ["abc"], "paths": ["owned.py"]}}
    verdict = {"regressed": [{"id": "R3a", "paths": ["owned.py"], "reason": "real defect"}]}
    assert verdict_authorship_errors(verdict, authorship) == []


def test_bare_or_unlocated_regressions_fail_closed() -> None:
    authorship = {"R3a": {"commits": ["abc"], "paths": ["owned.py"]}}
    errors = verdict_authorship_errors({"regressed": ["R3a", {"id": "R3a"}]}, authorship)
    assert len(errors) == 2
    assert "no located authored paths" in errors[0]
    assert "has no paths" in errors[1]


def test_worker_graded_and_typed_gates_are_scoped_to_the_ticket_base() -> None:
    src = SCRIPT.read_text()
    assert "TICKET-AUTHORED SCOPE IS THE GATE BOUNDARY" in src
    assert "record the current HEAD as the ticket base" in src
    assert "minimalism-dry, typed-and-linted" in src
    assert "A finding in a path absent from that diff" in src


def test_post_merge_verdicts_are_checked_against_deterministic_authorship() -> None:
    src = SCRIPT.read_text()
    assert "agent_factory.ticket_authorship --repo" in src
    assert "verdict_authorship_errors" in src
    assert "THIS AMENDS STEP 4'S EARLIER EXACT-SCHEMA WORDING" in src
    assert "every object in regressed MUST add a REQUIRED paths field" in src
