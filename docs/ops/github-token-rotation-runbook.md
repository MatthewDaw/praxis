# GitHub token rotation & revocation runbook

The productivity feature (`R3`/`R2`/`R4` — repo discovery, commit-history
fetch, cached rollups) authenticates to the GitHub API with a single
org-scoped personal access token, read from the `GITHUB_PRODUCTIVITY_TOKEN`
environment variable / secret. This token is never logged, persisted, or
echoed in a response body — see `knowledge/serve/github_audit.py`, the sole
write path for recording that it was used (`record_github_token_use`), which
records only `timestamp` / `endpoint` / `repo_count`.

## Owner

The **Praxis backend on-call owner** (rotating; see the team's on-call
schedule) is the named owner of this token: they hold mint/rotate/revoke
authority in the GitHub org settings and are the contact for a suspected
leak.

## Rotation cadence

- **Scheduled:** every 90 days.
- **Forced:** immediately, whenever `docs/ops/github-token-rotation-runbook.md`'s
  revocation procedure below is invoked (leak, offboarding, or the
  productivity feature's kill switch is engaged for a security reason).

## Revocation runbook

1. In GitHub org settings → Developer settings → Personal access tokens,
   revoke the current token immediately. This fails every subsequent
   productivity request closed (the feature's server-side kill switch —
   `R39` — additionally lets the route/tab be disabled without a redeploy).
2. Mint a replacement token scoped to read-only repo/commit access on only
   the repositories the productivity feature needs.
3. Update the `GITHUB_PRODUCTIVITY_TOKEN` secret in the deployment's secret
   store (never a `.env` committed to git) and redeploy/restart so the new
   value is picked up.
4. Confirm recovery: issue one productivity request and verify a fresh audit
   entry is recorded via `knowledge.serve.github_audit.read_audit_log()`.
5. Record the rotation (who, when, why) in the team's incident/ops log.

## Audit trail

Every use of the token records one entry — `timestamp`, `endpoint`,
`repo_count` — to the audit log (`knowledge/serve/data/github_audit.log` by
default, overridable via `GITHUB_AUDIT_LOG_PATH`). The token value itself is
never a field on the audit entry, and `github_audit._redact` defensively
scrubs any token-shaped substring that might otherwise land in the one
free-text field (`endpoint`). Use `github_audit.contains_token_leak()` to
verify the log is clean.
