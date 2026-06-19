"""Materialize a RepoTask into a working dir (clone, checkout, bring fixed tests).

Network happens here (clone/fetch into a commit-keyed cache), at setup — so the
agent run itself stays offline. ``materialize_base`` produces the agent's start
state: source at ``base_commit`` with the PR's *tests* from ``target_commit``
applied. ``apply_gold`` simulates the gold fix (for the agent-free probe).
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from knowledge.evals.repo.repo_task_def import RepoTask


def _run(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout


def _cache_dir(repo: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", repo)
    return Path(tempfile.gettempdir()) / "praxis-repo-cache" / safe


def clone_cached(task: RepoTask) -> Path:
    """Clone (or refresh) the repo into a shared cache; return the cache path."""
    cache = _cache_dir(task.repo)
    if (cache / ".git").exists():
        _run(["git", "fetch", "--quiet", "--all"], cwd=cache)
    else:
        cache.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--quiet", task.clone_url(), str(cache)])
    return cache


def materialize_base(task: RepoTask, dest: Path) -> Path:
    """Lay down the agent's start state in ``dest``: base source + target tests."""
    cache = clone_cached(task)
    dest.mkdir(parents=True, exist_ok=True)
    # Local clone from the cache is fast and offline-after-cache.
    _run(["git", "clone", "--quiet", str(cache), str(dest)])
    _run(["git", "checkout", "--quiet", task.base_commit], cwd=dest)
    # Bring the PR's fixed tests onto the base checkout (the FAIL_TO_PASS oracle).
    for path in task.test_paths:
        _run(["git", "checkout", task.target_commit, "--", path], cwd=dest)
    return dest


def apply_gold(task: RepoTask, dest: Path) -> None:
    """Bring everything to ``target_commit`` — the gold fix (agent-free probe only)."""
    _run(["git", "checkout", task.target_commit, "--", "."], cwd=dest)
