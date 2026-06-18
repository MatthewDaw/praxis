---
name: sync-dev-with-gitlab-main
description: >-
  Fetches GitLab main and merges origin/main into the current PRAXIS dev branch
  so local work stays current with the team. Use when starting a session, before
  opening an MR, when the user runs /sync-with-main, or when they ask to sync
  with main, update their dev branch, or stay current with GitLab.
---

# Sync PRAXIS dev branch with GitLab main

## Context

- **Remote:** `origin` → `https://labs.gauntletai.com/monicapeters/praxis.git`
- **Canonical branch:** `main` (`origin/main`)
- **Dev branches:** pillar/feature branches (e.g. `monica/dashboard-human-gate`); never commit WIP directly to `main`

## When to run

- Start of a work session
- Before opening a merge request
- After a teammate merges to `main` and you need their changes locally

## Core workflow

Run from the repository root while on your **dev branch**:

```powershell
git fetch origin main
git merge origin/main
```

If `main` has moved, that brings those commits into your dev branch. If it's already current, Git will say "Already up to date."

## Agent checklist

1. **Inspect state:** `git branch -vv`, `git status`
2. **Branch guard:** If on `main`, checkout the dev branch first — do not merge `main` into itself
3. **WIP:** Unstaged changes are usually fine; if merge fails due to local edits, report and offer stash (only with user approval)
4. **Sync:**

```powershell
git fetch origin main
git merge origin/main
```

5. **Verify:** `git log HEAD..origin/main` must be empty
6. **Report:** List incoming commits (`git log --oneline @{u}..HEAD` or merge output) or confirm already current
7. **Push:** Only if the user explicitly requests `git push origin <branch>`

## Optional: update local `main` ref

Without checking out `main`:

```powershell
git fetch origin main:main
```

## Conflict resolution

1. Stay on the **dev branch**
2. Fix conflicted files, `git add`, then `git merge --continue`
3. Never resolve sync conflicts by merging dev into `main`

## Invariants

| Do | Don't |
|----|-------|
| `origin/main` → dev branch | dev branch → `main` (local merge for "sync") |
| Open MRs targeting `main` | Push dev-only history to `main` |
| Resolve conflicts on dev | Commit conflict fixes on `main` |

## PowerShell note

On Windows PowerShell 5.x, chain with `;` not `&&`.

## Related project config

- Rule: `.cursor/rules/praxis-git-sync.mdc`
- Command: `/sync-with-main`
- MR workflow: `.cursor/rules/praxis-shared.mdc` (conventional commits, peer review)
