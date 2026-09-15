"""AF_PROJECT_PATHS scopes the loop's own commits and its verifier to the project's paths.

mvpvue lives in mvpvue/ of a Python ML repository. The post-merge verifier ran that repository's
Python suite, which wrote campaign_state/ml_registry blobs into the build worktree, and commit_wip's
`git add -A` committed them into the build (2026-09-15). These tests run the SHIPPED commit_wip.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"


def _function(name: str) -> str:
    text = SCRIPT.read_text()
    start = text.index(f"\n{name}(){{")
    end = text.index("\n}\n", start)
    return text[start + 1 : end + 3]


def _sh(cmd: str, cwd: Path, **env: str) -> str:
    return subprocess.run(["bash", "-c", cmd], cwd=cwd, capture_output=True, text=True, check=True,
                          env=dict(os.environ, **env)).stdout.strip()


def _repo(tmp: Path) -> Path:
    r = tmp / "repo"
    (r / "mvpvue").mkdir(parents=True)
    _sh("git init -q -b build && git config user.email t@t && git config user.name t "
        "&& echo base > mvpvue/a.txt && git add -A && git commit -q -m base", r)
    (r / "mvpvue" / "new.ts").write_text("export const x = 1\n")
    (r / "campaign_state").mkdir()
    (r / "campaign_state" / "blob").write_text("junk\n")
    return r


def _wip(repo: Path, **env: str) -> None:
    prog = (f"WT={repo}\nsay(){{ :; }}\n" + _function("scrub_test_results") + "\n"
            + _function("commit_wip") + "\ncommit_wip")
    _sh(prog, repo, **env)


def test_scoped_sweep_commits_only_the_project_paths(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _wip(repo, AF_PROJECT_PATHS="mvpvue .github/workflows")
    committed = _sh("git show --name-only --format= HEAD", repo).split()
    assert committed == ["mvpvue/new.ts"]
    assert (repo / "campaign_state" / "blob").exists()
    assert _sh("git status --porcelain", repo) == "?? campaign_state/"


def test_unset_keeps_the_whole_tree_sweep(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _wip(repo)
    assert sorted(_sh("git show --name-only --format= HEAD", repo).split()) == [
        "campaign_state/blob", "mvpvue/new.ts"]


def test_nothing_in_scope_means_no_commit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "mvpvue" / "new.ts").unlink()
    head = _sh("git rev-parse HEAD", repo)
    _wip(repo, AF_PROJECT_PATHS="mvpvue")
    assert _sh("git rev-parse HEAD", repo) == head


def test_the_verifier_is_told_the_project_scope() -> None:
    text = SCRIPT.read_text()
    assert "Tickets just merged: $ids_csv.$scope_note Each was built" in text
    assert "THIS PROJECT'S CODE IS ONLY UNDER: ${AF_PROJECT_PATHS}" in text
