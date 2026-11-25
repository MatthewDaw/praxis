#!/usr/bin/env python3
"""Prepare source repairs in isolated code lanes without interrupting the pinned audit."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

import importlib.util

runner_path = Path(__file__).with_name("audit-supervisor.py")
spec = importlib.util.spec_from_file_location("audit_runner", runner_path)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

ROOT = runner.ROOT
WORKSPACE = Path.home() / "orca/workspaces/sports_analysis"
REVIEW = WORKSPACE / "website-2"
OPS = Path.home() / "backups/mvpvu-stack-ops"
CANDIDATES = ROOT / "repair-candidates"
SAFE_SOURCE = re.compile(r"mvpvu/(apps/(web|api)/src|packages/(contracts|domain)/src)/[A-Za-z0-9_./-]+$")
SKIP_SENSITIVE = re.compile(r"(^|/)(auth|authorization|security|permissions|access)(/|$)", re.IGNORECASE)
MAX_ATTEMPTS = 3
TARGET = "consolidation"   # the branch land.sh fast-forwards into
MAX_OPEN_LANES = 3        # never hold more repair worktrees than this at once


def git(directory, *args):
    return subprocess.check_output(["/usr/bin/git", "-C", directory, *args], text=True, timeout=20).strip()

def retry_delay(attempts):
    return 60 * 2 ** min(max(attempts - 1, 0), 4)

def runtime_revision():
    files = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (Path(__file__), runner_path)]
    tools = [(str(path.resolve()), path.stat().st_mtime_ns, path.stat().st_size)
             for path in (runner.CODEX, runner.NODE)]
    return hashlib.sha256(json.dumps([files, tools, runner.TOOL_PATH]).encode()).hexdigest()[:16]

def due(state, now, revision):
    if not state:
        return True
    if state.get("state") == "retry_exhausted":
        return revision != state.get("runtime_revision")
    if state.get("state") not in {"interrupted", "preparing"}:
        return False
    return ((state.get("attempts", 0) < MAX_ATTEMPTS or revision != state.get("runtime_revision"))
            and now >= state.get("retry_at", state.get("started", 0) + 60))

def changes(lane):
    tracked = git(lane, "diff", "--name-only", "HEAD").splitlines()
    untracked = git(lane, "ls-files", "--others", "--exclude-standard").splitlines()
    return sorted(set(tracked + untracked))

def eligible(files):
    return all(SAFE_SOURCE.fullmatch(file) and not SKIP_SENSITIVE.search(file) for file in files)


def retryable(error):
    # Timeouts are safe to resume once the OS lease and lane identity agree.
    # A rejected or unknown model/tool exit needs review rather than repetition.
    return isinstance(error, subprocess.TimeoutExpired) or str(error) == "Bounded Codex turn expired"


def prepare(path, lease, revision):
    finding = json.loads(path.read_text())
    case_id = finding["case_id"]
    if not runner.SECURE_ID.fullmatch(case_id) or finding["build"] != runner.BUILD:
        raise RuntimeError("Invalid repair evidence identity")
    status_path = CANDIDATES / f"{case_id}.json"
    previous = json.loads(status_path.read_text()) if status_path.exists() else {}
    if finding.get("coordinator_claimed"):
        runner.write_json(status_path, {**previous, "case_id": case_id, "evidence_build": runner.BUILD,
                                        "finding": str(path), "state": "coordinator_claimed"})
        return
    if not due(previous, time.time(), revision):
        return
    lane_name = f"audit-repair-{case_id.lower()}"
    lane = WORKSPACE / f"lane-{lane_name}"
    base = previous.get("base") or git(REVIEW, "rev-parse", "consolidation")
    state = {**previous, "case_id": case_id, "evidence_build": runner.BUILD,
             "finding": str(path), "state": "preparing", "started": time.time(),
             "lane": str(lane), "base": base, "runtime_revision": revision,
             "attempts": (previous.get("attempts", 0) if previous.get("runtime_revision") == revision else 0) + 1}
    state.pop("retry_at", None)
    state.pop("reason", None)
    turn_dir = ROOT / "turns" / f"repair-{case_id}-{time.time_ns()}"
    turn_dir.mkdir(mode=0o700)
    state["turn"] = str(turn_dir)
    runner.write_json(status_path, state)
    try:
        if lane.exists():
            owned = (previous.get("lane") == str(lane) and not lane.is_symlink()
                     and git(lane, "rev-parse", "--show-toplevel") == str(lane)
                     and git(lane, "symbolic-ref", "--short", "HEAD") == f"lane-{lane_name}"
                     and git(lane, "rev-parse", "HEAD") == base)
            if not owned or not eligible(changes(lane)):
                state.update(state="blocked", reason="Existing lane identity or diff needs coordinator review.")
                return
        else:
            with (turn_dir / "lane.log").open("wb") as log:
                subprocess.run(["/bin/bash", OPS / "new-lane.sh", lane_name, base],
                               cwd=REVIEW, stdout=log, stderr=subprocess.STDOUT,
                               env={**os.environ, "PATH": runner.TOOL_PATH},
                               timeout=150, check=True)
        prompt = f"""Prepare a narrowly scoped functional MVPVU repair in {lane}.
The canonical original-build audit evidence is {path}. Read it and read the adjacent product source.
Consolidation base is {base}. Only this new repair lane is writable for source.
Handle only ordinary application defects. For security, authorization behavior, a migration, external providers, data or unknown product intent, write an exact blocked reason in {turn_dir}/handoff.md and stop.
Inspect saved uncommitted changes before writing and preserve prior work. Check the latest consolidation source to see if this already landed. Preserve the latest owner requirements and related caller behavior. A confirmed historical URL that the owner replaced is superseded; report that rather than restoring retired UI. Add or update a focused test when it catches this defect. Do not touch review data, deployments, refs, commits, or other lanes.
Report exact files, targeted verification, and the observable focused recheck to run after the coordinator lands.
Write {turn_dir}/handoff.md before ending. No ungrounded success claim.
"""
        runner.model_turn(turn_dir, prompt, cwd=lane, extra_write=[lane], seconds=450, lease=lease)
        files = changes(lane)
        state.update(files=files, notes=str(turn_dir / "handoff.md"))
        if files and eligible(files):
            git(lane, "diff", "--check")
            state["state"] = "coordinator_review"
        else:
            state["state"] = "blocked"
            state["reason"] = "No eligible source repair; inspect the notes before applying anything."
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        if not retryable(error):
            state.update(state="blocked", reason=f"Repair needs coordinator review ({type(error).__name__}).")
        elif state["attempts"] >= MAX_ATTEMPTS:
            state.update(state="retry_exhausted",
                         reason=f"Repair exhausted {MAX_ATTEMPTS} attempts ({type(error).__name__}).")
        else:
            state.update(state="interrupted", reason=type(error).__name__,
                         retry_at=time.time() + retry_delay(state["attempts"]))
    finally:
        runner.write_json(status_path, state)


def landed(branch):
    """True when the lane branch is fully contained in the landing target."""
    return subprocess.run(["/usr/bin/git", "-C", str(REVIEW), "merge-base", "--is-ancestor", branch, TARGET],
                          capture_output=True, timeout=20).returncode == 0


def remove_lane(lane, branch):
    subprocess.run(["/usr/bin/git", "-C", str(REVIEW), "worktree", "remove", "--force", str(lane)],
                   capture_output=True, timeout=120)
    # -d (not -D) refuses unless the branch is fully landed: the safety check.
    subprocess.run(["/usr/bin/git", "-C", str(REVIEW), "branch", "-d", branch], capture_output=True, timeout=20)
    subprocess.run(["/usr/bin/git", "-C", str(REVIEW), "worktree", "prune"], capture_output=True, timeout=20)


def reap_lanes():
    """Every wake: delete the worktree of each repair that has landed or was given up.

    Only lanes this queue created (recorded in its own candidate files) are touched. A landed lane is
    removed with its branch. A lane that is blocked, exhausted or abandoned keeps its BRANCH (commit any
    work first) but loses its worktree, so disk is reclaimed and nothing unlanded is lost.
    Returns the number of repair worktrees still open."""
    open_lanes = 0
    for candidate in CANDIDATES.glob("*.json"):
        try:
            state = json.loads(candidate.read_text())
        except ValueError:
            continue
        lane = Path(state.get("lane", ""))
        if not state.get("lane") or not lane.exists() or state.get("worktree_removed"):
            continue
        branch = f"lane-{lane.name[len('lane-'):]}" if lane.name.startswith("lane-") else lane.name
        try:
            finished = landed(branch)
            given_up = state.get("state") in {"retry_exhausted", "abandoned", "landed", "superseded"}
            if finished or given_up:
                if not finished:
                    dirty = git(lane, "status", "--porcelain", "--untracked-files=no")
                    if dirty:
                        git(lane, "add", "-u")
                        git(lane, "commit", "-q", "-m", f"WIP: repair {candidate.stem} saved before worktree cleanup")
                remove_lane(lane, branch)
                state.update(worktree_removed=time.time(),
                             state="landed" if finished else state.get("state"))
                runner.write_json(candidate, state)
                continue
        except (OSError, subprocess.SubprocessError):
            pass
        open_lanes += 1
    return open_lanes


if __name__ == "__main__":
    if runner.PAUSE.exists():
        raise SystemExit(0)
    with (ROOT / "repair.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(0)
        CANDIDATES.mkdir(mode=0o700, exist_ok=True)
        revision = runtime_revision()
        if reap_lanes() >= MAX_OPEN_LANES:
            raise SystemExit(0)   # land or give up open repairs before creating more worktrees
        for request in sorted(runner.HANDOFF.glob("*.json"), key=lambda path: path.stat().st_mtime):
            candidate = CANDIDATES / request.name
            state = json.loads(candidate.read_text()) if candidate.exists() else {}
            if due(state, time.time(), revision):
                prepare(request, lock, revision)
                break
