"""Pin what `af_owned_ids` actually accepts as a ticket id.

Integration merges a worker branch only if it can name the ticket its commits
belong to, and it learns that from a TRAILING `(ID)` in the commit subject. The
rule is easy to get wrong in a way that looks right: a conventional-commit scope
puts a plausible-looking id at the FRONT, and it does not count.

That cost real work on 2026-08-06 — appeal_engine COV-1B produced 42 files and
~1800 insertions of passing code whose subjects read `feat(cov1b): ...`, and the
orchestrator declined to merge any of it.

These tests exercise the shell function itself against a real git history, so
they fail if the regex is ever loosened or tightened without thought.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

LOOP = Path(__file__).resolve().parent.parent / "scripts" / "af-ticket-loop.sh"


def _extract(tmp_path: Path, subjects: list[str], known_ids: str) -> str:
    """Build a throwaway repo with these commit subjects, run af_owned_ids over it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(a, cwd=repo, capture_output=True, text=True, check=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@t.t")
    run("git", "config", "user.name", "t")
    (repo / "seed").write_text("seed")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "seed")
    run("git", "branch", "base")
    for i, subject in enumerate(subjects):
        (repo / f"f{i}").write_text(str(i))
        run("git", "add", "-A")
        run("git", "commit", "-q", "-m", subject)

    # Source only the function under test, then call it.
    script = (
        f"set -e\n"
        f"eval \"$(sed -n '/^af_owned_ids(){{/,/^}}/p' {LOOP!s})\"\n"
        f"cd {repo!s}\n"
        f"af_owned_ids 'base..HEAD' ' {known_ids} '\n"
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_trailing_id_is_extracted(tmp_path):
    got = _extract(tmp_path, ["feat(ocr): stand up the sidecar (COV-1B)"], "COV-1B")
    assert got == "COV-1B"


def test_conventional_commit_scope_is_not_an_id(tmp_path):
    """The exact shape that stranded COV-1B."""
    got = _extract(tmp_path, ["feat(cov1b): stand up the sidecar"], "COV-1B")
    assert got == "", "a leading scope must not be mistaken for a ticket id"


def test_lowercase_trailing_id_does_not_match(tmp_path):
    """Match is case-exact against the ticket's own requirement_id."""
    got = _extract(tmp_path, ["feat: stand up the sidecar (cov-1b)"], "COV-1B")
    assert got == ""


def test_unknown_id_is_ignored(tmp_path):
    """An id from another tracker is not this project's provenance."""
    got = _extract(tmp_path, ["fix: something (JIRA-42)"], "COV-1B")
    assert got == ""


def test_multiple_commits_yield_the_union(tmp_path):
    got = _extract(
        tmp_path,
        ["feat: a (COV-1B)", "chore: unrelated", "test: b (DATA-1)"],
        "COV-1B DATA-1",
    )
    assert set(got.split()) == {"COV-1B", "DATA-1"}


def test_one_labelled_commit_rescues_the_branch(tmp_path):
    """Provenance needs ONE good subject, not every subject — so a WIP-heavy
    branch still merges as long as something names the ticket."""
    got = _extract(
        tmp_path,
        ["wip: scratch", "wip: more scratch", "feat: the real thing (COV-1B)"],
        "COV-1B",
    )
    assert got == "COV-1B"


@pytest.mark.parametrize("subject", [
    "feat(scope): thing (COV-1B)",
    "fix: thing (COV-1B)   ",
])
def test_trailing_id_tolerates_scope_and_trailing_space(tmp_path, subject):
    assert _extract(tmp_path, [subject], "COV-1B") == "COV-1B"
