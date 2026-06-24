---
name: using-git-worktrees
author: obra/superpowers
source: https://skills.sh/obra/superpowers/using-git-worktrees
---

# using-git-worktrees

Isolated git worktrees with smart directory selection and safety verification.

## When to use
- Setting up isolated workspaces for feature branches or experimental work.
- Agent-driven workflows that need parallel environments.
- Needing automatic project setup detection and baseline testing.

## Workflow
1. Detection first — check for existing isolation before creating a new worktree.
2. Smart directory selection — `.worktrees`, CLAUDE.md preference, or user input; local or global storage.
3. Safety verification — confirm project-local worktree dirs are git-ignored.
4. Auto-setup — detect project type and run init (npm install, cargo build, pip install, go mod download).
5. Baseline testing — run tests to establish a clean state before proceeding.

Prefer platform-native isolation; fall back to manual git worktrees only when necessary.
