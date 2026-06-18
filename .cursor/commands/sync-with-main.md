---
name: sync-with-main
description: Fetch GitLab main and merge origin/main into the current dev branch so it stays current before work or an MR.
---

# Sync with GitLab main

Follow the project skill at `.cursor/skills/sync-dev-with-gitlab-main/SKILL.md`.

## Quick steps

1. Show current branch and `git status` (note unstaged WIP; stash only if the user asks).
2. Refuse to sync if currently on `main` — checkout the user's dev branch first (ask which branch if unclear).
3. Run:

```powershell
git fetch origin main
git merge origin/main
```

4. Verify: `git log HEAD..origin/main` should be empty after a successful sync.
5. Summarize what landed (commit subjects) or confirm "Already up to date."
6. Do **not** push unless the user explicitly asks.

If the user also wants local `main` updated without switching branches:

```powershell
git fetch origin main:main
```
