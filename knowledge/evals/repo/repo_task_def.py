"""Shape of a real-repo (SWE-bench-style) code task.

A ``RepoTask`` pins a real GitHub project at a commit and names the tests that
should flip. The PR's gold diff is never stored — it is derived from the two
commits at materialization time (bring the fixed tests from ``target_commit``
onto the ``base_commit`` checkout; the source stays buggy for the agent to fix).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RepoTask(BaseModel):
    repo: str  # "owner/name" or a full clone URL
    base_commit: str  # repo state before the fix (the agent starts here)
    target_commit: str  # the merge commit — reference/oracle only, never shown to the agent
    test_paths: list[str] = Field(default_factory=lambda: ["tests/"])  # paths whose fixed version is brought from target
    fail_to_pass: list[str] = Field(default_factory=list)  # pytest node ids that must flip RED->GREEN
    pass_to_pass: list[str] = Field(default_factory=list)  # node ids that must stay GREEN

    def clone_url(self) -> str:
        if self.repo.startswith(("http://", "https://", "git@")):
            return self.repo
        return f"https://github.com/{self.repo}.git"
