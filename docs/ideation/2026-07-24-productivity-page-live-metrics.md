# Ideation — Productivity Page: fetching metrics live & fast

- **Date:** 2026-07-24
- **Mode:** repo-grounded, focused research pass (user asked for "a little research")
- **Topic:** How to fetch, LIVE and FAST, the three productivity-page metrics —
  (1) LOC committed in the last day, (2) commit count, (3) finished Praxis tickets —
  aggregated across **all** repos the user works on (owned public + private, and private
  repos they only contribute to), across GitHub and (per README) GitLab.
- **Grounding:** web research on GitHub/GitLab API mechanics + codebase grounding on
  Praxis ticket-state, tab model, auth, and existing git integration.

---

## Headline findings (the two facts that reshape the plan)

1. **Finished-ticket count already exists in Praxis — as a point-in-time count, not a time series.**
   `GET /requirements/completeness?project=<p>` returns `{total_active_requirements, complete,
   incomplete, breakdown}` where `complete` = finished tickets (`app.py:2944`, backed by
   `completeness_summary()`). A ticket = fact `category="requirement"` with
   `meta.build_state ∈ {incomplete|in_progress|finished|blocked}`; `finished` ⇒ complete.
   **Gaps:** it is (a) **per-project** — no cross-space aggregate; (b) **point-in-time** — there is
   **no `finished_at` timestamp** anywhere, so "tickets finished *in the last day*" is NOT queryable
   today. Facts have `created_at`; episodes carry `decided_at`; neither records ticket finish-time.

2. **There is ZERO git integration server-side today — this half of the page is greenfield.**
   No `octokit`/`PyGithub` dep, no `GITHUB_TOKEN`/`GITLAB_TOKEN` in `.env.example`, no stored user
   git credentials, no outbound-git endpoint. The only git-shaped code (`knowledge/injestion/pr_source.py`)
   shells to a **local** `gh` CLI for offline backfill — explicitly *not* a deployed dependency.
   ⇒ Every commit/LOC number requires new outbound-API code AND a new place to hold a git token.

---

## Data-acquisition options (generated → critiqued → survivors)

### Metric: commit count + LOC in last 24h (GitHub)

**★ SURVIVOR — S1. GraphQL `history` connection (RECOMMENDED FAST PATH).**
One query per active repo (aliasable to batch dozens in a single round trip):
`repository.defaultBranchRef.target.history(since, until){ nodes{ additions deletions committedDate
author{user{login}} } }`. Gets **commit count AND additions/deletions in the SAME response**, with true
daily granularity, and **covers private repos**. ~5000 GraphQL points/hr. This is the only mechanism that
delivers both target metrics, at day granularity, over private repos, in the fewest requests.
- *Basis:* `direct:` confirmed working query shape; `reasoned:` single round-trip = lowest latency for a live page.
- *Caveat (must go in requirements as a stated limitation):* counts **default-branch commits only** and
  **excludes forks** — same caveat as every GitHub aggregate. Feature-branch work not yet merged won't count.

**★ SURVIVOR — S2. `contributionsCollection.commitContributionsByRepository` as the discovery step.**
One cheap query returns exactly *which repos had activity in the window* (incl. private, if the user enables
"Private contributions" visibility — else they collapse to opaque `restrictedContributionsCount`). Use it to
avoid running S1 against every repo from `/user/repos`; only hit repos that actually changed.
- *Basis:* `direct:` returns per-repo commit contributions for a date range in 1 query.
- *Caveat:* private breakdown requires the profile visibility toggle; no LOC field (pairs with S1 for LOC).

**REJECTED — R1. `GET /search/commits` (`author:<login> author-date:>=…`).** Searches default branch only,
**30 req/min** search rate limit, **1000-result cap**. Too fragile as a repeated live source, and gives no LOC.
*Reason:* rate/ cap fragility + no LOC.

**REJECTED — R2. `GET /repos/{o}/{r}/stats/contributors`.** Weekly buckets only (no daily), async **202
"retry later"**, returns all-zeros for repos ≥10k commits. Structurally wrong for "last day."
*Reason:* weekly granularity + 202 + zero-fill.

**REJECTED — R3. Events API `GET /users/{u}/events` (PushEvent).** **Public activity only**, ~300-event/90-day
cap. Violates the private-repo hard requirement. *Reason:* no private coverage.

**REJECTED — R4. Per-commit REST `GET …/commits/{sha}` for stats.** O(commits) fan-out on the 5000/hr core
pool; S1 gets the same additions/deletions inline. *Reason:* strictly dominated by S1.

### Metric: finished Praxis tickets

**★ SURVIVOR — S3. Reuse `GET /requirements/completeness` per project; `complete` = finished.**
Zero new backend logic for the *point-in-time* count. For a cross-project total, loop `GET /spaces`
(`app.py:903`) × completeness, or add a thin cross-space aggregate route.
- *Caveat / OPEN DECISION:* the ask says "finished **in the last day**" — completeness has **no time window**.
  Either (a) redefine the metric as *current finished-ticket count* (cheap, ships now), or (b) add a
  `finished_at` signal + time-window query (new backend work, new fact/episode timestamp). This is the single
  biggest scoping fork on the Praxis side.

### Cross-cutting: where does the git token live? (the real architectural fork)

**S4. Backend-held git token (server calls GitHub GraphQL, dashboard calls Praxis).** Matches the existing
pattern — React already calls the FastAPI backend with a Cognito bearer; add a `/productivity` route (next to
`/requirements/completeness`, same `current_user`/`active_org` deps) that fans out to GitHub server-side and
caches. **Token is a stored credential** ⇒ high-regret item (must surface live even in Autonomous mode):
per-user vs single-owner token, encryption at rest, scopes (classic `repo` for private-contrib discovery).

**S5. Client-held token (browser calls GitHub directly).** No server secret storage; but exposes the PAT to
the browser, spreads rate-limit/CORS handling into the frontend, and can't cache across users. Weaker.

**S6. Caching layer (applies to whichever of S4/S5 wins).** "Click and see fresh" ≠ real-time. Short-TTL
server cache (60–120s); GitHub REST supports ETag `304` (free), GraphQL has no conditional requests so TTL is
the only lever. Prevents a page-load from burning the rate budget.

---

## GitLab (second host — smaller, noted)
- Discovery: `GET /projects?membership=true` (public+private). Commit LOC: `GET /projects/:id/repository/commits/:sha`
  returns `stats{additions,deletions}` **inline by default** (no weekly-only endpoint). Push events:
  `GET /events?action=pushed` — **but bulk/multi-ref pushes collapse to `commit_count:0`**, silently
  undercounting. Separate PAT (`read_api`). ⇒ GitLab is a *second integration*, not a free add-on.

## Identity resolution (both hosts)
`Commit.author.user.login` (GraphQL) resolves "mine" directly when the commit email is a verified account email;
fallback = match raw author email against `GET /user/emails`. Requirements doc must name the canonical identity.

---

## Recommended fast path (for the requirements doc / af-intake)
1. **Tickets:** ship S3 as *current finished count* now; flag time-window ("last day") as an open decision (needs `finished_at`).
2. **Commits + LOC:** S2 (discover active repos) → S1 (GraphQL history for count + additions/deletions), private-repo capable.
3. **Architecture:** S4 backend-held token + S6 short-TTL cache. Token storage = high-regret, surface to human.
4. **Scope order:** GitHub first; GitLab as an explicit phase-2 integration (own token, own quirks).

## Open decisions this raises (hand to af-plan → af-intake)
- "Last day" for tickets: redefine as current-count, or build `finished_at` + time window? (biggest fork)
- Token: per-user or single-owner? backend-stored (encrypted) or client-supplied? scopes?
- Private-contribution visibility toggle dependency — acceptable, or need a fallback?
- Default-branch-only / fork-exclusion caveat — acceptable as stated limitation?
- Cross-project ticket aggregate vs single active project?
- GitHub-only v1, or GitLab in scope for v1?
- Cache TTL / "fresh" definition; empty/error/rate-limited states on the page.
