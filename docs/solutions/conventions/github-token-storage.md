---
title: GitHub token storage — rotation cadence, owner and revocation runbook
date: 2026-07-25
category: conventions
module: knowledge/serve (github_audit), infra/lib/backend-service-stack.ts
problem_type: convention
component: secrets_management
severity: high
related_components: [productivity_feature, secrets_manager, audit_logging]
applies_when:
  - Rotating, revoking or auditing the backend's GitHub personal access token
  - Adding a new caller of the GitHub token (e.g. the productivity route's GraphQL client, R2/R3)
  - Investigating a suspected token leak
tags: [security, secrets, github, operations, audit-log, token-rotation]
---

# GitHub token storage

The productivity feature (R1–R11) reads repository activity via a single
backend-held GitHub personal access token (PAT), stored in AWS Secrets Manager
per `infra/lib/backend-service-stack.ts` (R1). This doc is the operational
half: who owns the token, how often it rotates, how to revoke it, and how its
use is audited.

## Rotation cadence and owner

- **Cadence:** the token is rotated every **90 days**, or immediately on
  suspected exposure (see Revocation below).
- **Owner:** Matt Daw (`mattdaw7@gmail.com`) — the sole GitHub account whose
  PAT backs this feature (the same account the productivity series' commits
  are attributed to, per R11). The owner is responsible for minting the
  replacement token (`Contents: Read` only, no other scopes) and updating the
  Secrets Manager secret; the backend picks up a rotated value on its next
  authentication failure with no redeploy (R1).

## Revocation runbook

If the token is suspected leaked or is being rotated on schedule:

1. **Kill the feature first, not last** — flip the productivity kill switch
   (R39) so the route and the dashboard tab stop calling GitHub immediately,
   containing any further blast radius while the token is still valid.
2. **Revoke the token** at https://github.com/settings/tokens (or via `gh
   api -X DELETE /authorizations/<id>` for a classic PAT) — this invalidates
   it at GitHub regardless of what the backend still has cached.
3. **Mint a replacement** — a new fine-grained PAT scoped to `Contents: Read`
   only on the repositories the feature needs, owned by the same named owner
   above.
4. **Update the secret** in AWS Secrets Manager (the secret `grantRead` onto
   the App Runner instance role, per R1) — no code change or redeploy needed.
5. **Confirm pickup** — the backend's in-process token cache refreshes on an
   authentication failure, so the first request after the update re-fetches
   the new value; confirm via the audit log (below) that subsequent entries
   keep flowing with no error spike.
6. **Re-enable the feature** once the new token is confirmed working.

## Audit log

Every backend call that spends the GitHub token is recorded by
`knowledge.serve.github_audit.record_github_use(endpoint, repository_count)`
(called by the productivity route's GitHub client, R2/R3) as one JSON line on
the `github.audit` logger — timestamp, endpoint and repository count only.
The token value is never a field on this entry, and the module additionally
redacts a `github_pat_`/`ghp_`-prefixed value if one is ever accidentally
interpolated into `endpoint`, so no code path can put the raw token into logs.
In production this reaches App Runner's CloudWatch log group, the durable
store an owner searches when confirming step 5 above or investigating a
suspected leak — a search for `github_pat_` there should always come back
empty.
