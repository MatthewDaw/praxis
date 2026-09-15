"""A Grok worker clone's UNCOMMITTED edits must reach the build repo, named for its ticket.

Grok spawn_subagent builds in a separate clone under ~/.grok/worktrees/workspace-<checkout>/subagent-*.
Committed work there was salvaged; uncommitted edits were on no branch and never left the clone, so a
retry could not continue them. These tests run the SHIPPED functions against real repositories.
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


def _sh(cmd: str, cwd: Path, home: Path | None = None) -> str:
    env = None
    if home is not None:
        import os
        env = dict(os.environ, HOME=str(home))
    return subprocess.run(["bash", "-c", cmd], cwd=cwd, capture_output=True, text=True,
                          check=True, env=env).stdout.strip()


def _setup(tmp: Path) -> tuple[Path, Path, Path]:
    wt = tmp / "sports_analysis-mvpvue"
    wt.mkdir()
    _sh("git init -q -b build && git config user.email t@t && git config user.name t "
        "&& echo base > a.txt && git add a.txt && git commit -q -m base", wt)
    home = tmp / "home"
    clone = home / ".grok" / "worktrees" / f"workspace-{wt.name}" / "subagent-abc"
    clone.parent.mkdir(parents=True)
    _sh(f"git clone -q {wt} {clone} && git -C {clone} config user.email t@t && git -C {clone} config user.name t", tmp)
    return wt, home, clone


def _salvage(wt: Path, home: Path, ids: str) -> str:
    prog = (f"WT={wt}\nsay(){{ echo \"$*\"; }}\n" + _function("af_preserve_uncommitted") + "\n"
            + _function("salvage_external_grok_clones") + f'\nsalvage_external_grok_clones "{ids}"')
    return _sh(prog, wt, home)


def test_uncommitted_clone_edits_land_in_a_ticket_named_ref(tmp_path: Path) -> None:
    wt, home, clone = _setup(tmp_path)
    (clone / "data.ts").write_text("export const layer = 1\n")

    out = _salvage(wt, home, "T03")

    ref = "refs/af-preserved/grok-subagent-abc-T03-latest"
    assert "preserved UNCOMMITTED work from grok clone" in out
    assert _sh(f"git show {ref}:data.ts", wt) == "export const layer = 1"
    assert "RESUME" not in out  # salvage only preserves; resuming is af_resume_rule's job


def test_an_unchanged_clone_is_not_snapshotted_twice(tmp_path: Path) -> None:
    wt, home, clone = _setup(tmp_path)
    (clone / "data.ts").write_text("x\n")
    _salvage(wt, home, "T03")
    first = _sh("git rev-parse refs/af-preserved/grok-subagent-abc-T03-latest", wt)
    out = _salvage(wt, home, "T03")
    assert "preserved UNCOMMITTED" not in out
    assert _sh("git rev-parse refs/af-preserved/grok-subagent-abc-T03-latest", wt) == first


def test_a_multi_ticket_round_does_not_guess_the_ticket(tmp_path: Path) -> None:
    wt, home, clone = _setup(tmp_path)
    (clone / "data.ts").write_text("x\n")
    _salvage(wt, home, "T03,T04")
    assert _sh("git for-each-ref --format='%(refname)' refs/af-preserved", wt) == "refs/af-preserved/grok-subagent-abc-latest"
