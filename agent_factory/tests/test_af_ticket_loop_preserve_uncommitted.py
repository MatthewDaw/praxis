"""A timed-out worker's UNCOMMITTED edits must survive the worktree purge.

mvpvue round #5 (2026-09-14): the round hit its deadline with T03's worker ~56 minutes in and nothing
committed; sweep_worktrees purged the tree ("the branch is the artifact") and every edit was gone.
af_preserve_uncommitted snapshots the tree into refs/af-preserved/* first. These tests run the SHIPPED
function against a real repository.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"


def _function(name: str) -> str:
    text = SCRIPT.read_text()
    start = text.index(f"\n{name}(){{")
    end = text.index("\n}\n", start)
    return text[start + 1 : end + 3]


def _sh(cmd: str, cwd: Path) -> str:
    return subprocess.run(["bash", "-c", cmd], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def _repo(tmp: Path) -> tuple[Path, Path]:
    main = tmp / "main"
    main.mkdir()
    _sh("git init -q -b trunk && git config user.email t@t && git config user.name t "
        "&& echo base > a.txt && git add a.txt && git commit -q -m base", main)
    wt = tmp / "wt"
    _sh(f"git worktree add -q -b af-build/proj-T03 {wt} trunk", main)
    return main, wt


def _preserve(wt: Path) -> str:
    return _sh(_function("af_preserve_uncommitted") + f'\naf_preserve_uncommitted "{wt}"', wt)


def test_uncommitted_edits_are_preserved_in_a_ref_without_touching_the_tree(tmp_path: Path) -> None:
    main, wt = _repo(tmp_path)
    (wt / "a.txt").write_text("edited\n")
    (wt / "new.ts").write_text("export const x = 1\n")
    status_before = _sh("git status --porcelain", wt)

    ref = _preserve(wt)

    assert ref.startswith("refs/af-preserved/af-build-proj-T03-")
    assert _sh(f"git show {ref}:a.txt", main) == "edited"
    assert _sh(f"git show {ref}:new.ts", main) == "export const x = 1"
    assert _sh(f"git rev-parse {ref}^", main) == _sh("git rev-parse trunk", main)
    # the worker's branch, index and working files are exactly as they were
    assert _sh("git rev-parse af-build/proj-T03", main) == _sh("git rev-parse trunk", main)
    assert _sh("git status --porcelain", wt) == status_before


def test_a_clean_tree_preserves_nothing(tmp_path: Path) -> None:
    main, wt = _repo(tmp_path)
    assert _preserve(wt) == ""
    assert _sh("git for-each-ref refs/af-preserved", main) == ""


def test_sweep_preserves_before_it_purges() -> None:
    body = _function("sweep_worktrees")
    assert body.index("af_preserve_uncommitted") < body.index("af_force_remove_worktree")
