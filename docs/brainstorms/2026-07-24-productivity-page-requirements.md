# Requirements — Praxis Productivity Page

- **Date:** 2026-07-24
- **Author:** af-plan (explore/research front-end) — hand-off doc for af-intake-plan
- **Project / org:** `praxis`
- **Rigor mode:** **Rigorous** · **Decision mode:** **Autonomous (force-decisions)** — genuine forks
  are settled with a low-regret **default, flagged for override**; the human reviews them in one pass.
  Where the human has already directed a fork (git-key handling, range selector, 4-week default), it's
  recorded as **user-directed**, not defaulted.
- **Grounding:** `docs/ideation/2026-07-24-productivity-page-live-metrics.md` + codebase grounding
  (tab model, auth, ticket state, git-integration absence, ticket finish-time absence).
- **Passes run:** research-to-ground ✓, ce-ideate ✓, adversarial/edge enumeration ✓,
  ce-doc-review panel ⏳ (re-run after this revision; logged at end).

> **Boundary:** af-plan writes nothing to Praxis. af-intake-plan admits each settled requirement as a
> `source="prd-praxis"` fact, runs the audit, and saves the snapshot.

---

## 1. Scope

### In scope
A new **"Productivity"** tab in the dashboard top nav row (with Knowledge / Contradictions / Context /
MCP Setup). It shows the signed-in user's own productivity as a **daily line graph** over a
**selectable time range**, plus a **connect-free** git data source.

**The line graph plots these per-day series over the selected range:**
- **S1 — Lines added / day** (additions).
- **S2 — Lines deleted / day** (deletions).
- **S3 — Net lines / day** (additions − deletions; "net new lines" / "diff between added and removed").
- **S4 — Praxis tickets completed / day.**

  > Interpretation note (flag D14): the user listed "net new lines," "lines deleted," and "diff
  > between added and removed" — S1/S2/S3 cover all three readings. "New completed tickets in a day"
  > and "tickets completed per day" are treated as one series (S4). Override if a distinction was meant
  > (e.g. cumulative vs per-day).

**Time-range selector (dropdown)** controlling the graph's window and x-axis bucketing:
- **day** · **week** · **last 4 weeks (DEFAULT)** · **last 12 months (rolling, not calendar year)** · **all time**

**Data sources:**
- Commits / LOC (S1–S3): **GitHub** via a **backend-held, least-privilege, pre-provisioned key** —
  **no per-user setup, no connect flow** (user-directed: "Praxis should just have keys to my GitHub").
- Tickets (S4): **Praxis** itself, across the user's connected+contributed projects.

**Repo/project scope:** every project the user has **connected to Praxis and contributes to** — commit/
LOC series aggregate across the corresponding git repos; ticket series aggregates across the user's
Praxis spaces (see D4/D15 for the repo↔project mapping question).

### Explicitly out of scope (v1)
- Team/multi-user metrics (single-user, the signed-in user only).
- GitLab (deferred phase-2; D6) — GitHub only in v1.
- Non-git signals (PRs reviewed, issues), goals/streaks/targets, writing productivity data into the KG.
- Editing tickets or commits from this page (read-only).
- **Private repos owned by *other individuals* the user only contributes to** — excluded in v1 as a
  direct consequence of the user-confirmed least-privilege fine-grained token (D21); disclosed in the UI.

### Personas / actors
- **Signed-in Praxis user** (`mattdaw7@gmail.com`) — sole viewer, sees only their own metrics.
- **GitHub** — commit/LOC source (backend-held key).
- **Praxis backend** (`knowledge/serve`, App Runner) — computes/serves the series, holds the key, caches.
- **Praxis dashboard** (`frontend-react`) — renders the tab + chart + range dropdown.

---

## 2. Behaviors (with sketched acceptance conditions)

**B1 — Tab in the top nav row.** *Acceptance:* clicking selects `viewTab === "productivity"`; renders
the productivity panel; FilterBar hidden (like setup/context); `aria-selected` correct.

**B2 — Daily line graph renders S1–S4.** *Acceptance:* a line chart with one line per series (S1–S4),
x-axis = day buckets across the selected range, y-axis = count/lines; legend distinguishes series;
verifiable against a hand-count on a small test window.

**B3 — Range dropdown.** *Acceptance:* selecting day / week / last-4-weeks / last-12-months / all-time
re-queries and re-renders with the correct window; **default on open = last 4 weeks**; the selection is
reflected in the axis labels and (D16) persists across refresh within a session.

**B4 — Adaptive bucketing for long ranges (D17).** *Acceptance:* "day"/"week"/"4 weeks" bucket by day;
"12 months" and "all time" bucket by a coarser unit (default weekly for 12mo, monthly for all-time) so
the chart stays legible and the query stays within GitHub node limits. Bucketing is disclosed on the axis.

**B5 — Commit/LOC series from a backend-held key, zero user setup.** *Acceptance:* with the operator's
GitHub key configured server-side, opening the page shows S1–S3 with **no login-to-GitHub, no connect
button, no token paste**. If the key is absent/misconfigured, S1–S3 show a clear operator-facing "GitHub
key not configured" state (not a user connect prompt).

**B6 — Ticket series from Praxis.** *Acceptance:* S4 = count of tickets that reached `finished` on each
day, across the user's connected+contributed projects. Requires the new finish-timestamp signal (D1);
before instrumentation exists, S4 shows only data from the instrumentation date forward, labeled as such.

**B7 — Fresh on open + manual refresh + "last updated".** *Acceptance:* opening fetches; a Refresh
control re-fetches; a "last updated HH:MM" label shows cache age (TTL per D7).

**B8 — Scoped to the signed-in user only.** *Acceptance:* series reflect the authed user's own git
identity (D10) and own Praxis spaces; never another user's; enforced by existing
`current_user`/`active_org` deps.

**B9 — Least-privilege, read-only key.** *Acceptance:* the configured GitHub key is a fine-grained token
with **Contents: Read + Metadata: Read only**, no write, no other permissions (D2/D3). Documented in the
operator setup note; the key is never returned to the browser or logged.

---

## 3. Edge states & failure classes (over-generated)

**Chart/panel states:**
- **Loading** (first open + range change + refresh) — skeleton chart, not empty axes.
- **Empty window** — user had 0 activity in range → render a real zero-line, distinct from "not loaded"
  and "load failed."
- **Partial failure** — GitHub ok but Praxis ticket query fails (or vice-versa): render the series that
  loaded, per-series error badge for the rest. **Silent-partial-failure is a named risk** — a failed
  series must look different from a genuine flat-zero line.
- **Sparse/pre-instrumentation ticket data** — S4 flat/absent before finish-timestamps existed; label
  "ticket history starts <date>" so it doesn't read as "you finished nothing."
- **GitHub key missing/expired/insufficient-scope** — operator-facing message ("GitHub key not
  configured / expired / missing Contents:Read"), never a raw 401 and never a user connect prompt.
- **Rate-limited** (GraphQL points / large all-time range) — serve cached + "rate-limited, cached at
  HH:MM"; for all-time, coarse bucketing + caching is the primary mitigation.
- **Timeout / host down** (App Runner or GitHub) — bounded timeout, error state, retry.
- **Stale cache** — on live-fetch failure with cache present, show cached + age + stale marker.
- **Identity unresolved** (D10) — commits whose author email isn't linked to the account: count what
  resolves, disclose "N commits unattributed" rather than dropping silently.
- **Large fan-out** — user contributing to many repos / >100 commits/repo/day → discovery-first + cap +
  pagination; if a cap is hit, surface "showing first N," never silent truncation.
- **Long-range node-limit** — "all time" across many repos can exceed GitHub's 500k-node/query cap →
  chunk by time, coarse buckets, cache; disclose if truncated.
- **Timezone/day-boundary** — a "day" bucket's edge (D9); commits at midnight attributed consistently.

**Can't-miss failure classes:**
- **Credential exposure** — backend-held GitHub token is a bearer secret: encrypted at rest, never
  logged, never in a browser-visible response, never written to the KG. (User authorized backend
  storage; secure handling is mandatory.)
- **Silent partial failure / silent truncation** — explicitly designed against (above).
- **Wrong-tenant leak** — guarded by existing tenancy deps.
- **Misleading ticket history** — presenting pre-instrumentation zeros as real; guarded by B6 labeling.
- **Irreversible actions** — none (read-only page); confirm at af-intake.

---

## 4. Implied features (ce-ideate; accept/defer)
- **Ticket finish-event instrumentation** (ACCEPT, load-bearing, D1) — record a timestamped event when a
  ticket transitions to `finished`, so S4 has a real per-day history going forward.
- **Backend GitHub-key config + secret storage** (ACCEPT, load-bearing) — one-time operator provisioning
  (env/secret), no user UI.
- **Per-repo breakdown / hover tooltips** (DEFER, flag) — "which repos drove today's lines."
- **Cumulative totals alongside per-day** (DEFER, flag) — e.g. running total line.
- **Export CSV / share** (DEFER).
- **Configurable series toggles** (ACCEPT-lite, D18) — let the user show/hide S1–S4 lines.

---

## 5. Open decisions (for af-intake-plan) — defaults flagged; user-directed marked

| # | Fork | Resolution (flagged unless user-directed) | Basis |
|---|------|-------------------------------------------|-------|
| **D1** | Ticket "completed per day" history | **Build a finish-timestamp signal** (stamp on `finished` transition); S4 accrues from instrumentation date. **No reliable historical backfill** before that. | Grounding: only current `build_state` + `last_outcome`; no finish-time; facts lack an `updated_at`. **This is now mandatory, not deferred — the requested S4 line forces it.** |
| **D2** | Where the GitHub key lives | **USER-DIRECTED: backend-held, pre-provisioned, encrypted at rest.** No per-user connect flow. | User: "Praxis should just have keys to my GitHub." |
| **D3** | Token privilege | **USER-DIRECTED intent → fine-grained PAT, Contents:Read + Metadata:Read, read-only, no write, nothing else.** **VALIDATED against GitHub docs:** per-day additions/deletions require `Contents:Read` (`/commits`, per-commit, GraphQL `history`); `Metadata:Read` is the base permission; there is **no "commit-stats-only" permission** (the only Metadata-only LOC endpoint, `stats/code_frequency`, is weekly + repo-wide + all-authors — unusable for a per-day just-me series). So "no code reading" is **not satisfiable**; `Contents:Read` (read-only) is the floor. | GitHub REST fine-grained-PAT permissions reference. |
| **D21** | Token privilege ↔ coverage tradeoff (single no-setup key) | **USER-CONFIRMED: option (A) least-privilege.** One fine-grained PAT (`Contents:Read`+`Metadata:Read`, read-only), covering the user's own + org-approved repos; private repos owned by *other individuals* the user only contributes to are **EXCLUDED in v1 and disclosed in the UI**. Rejected: (B) classic `repo`-scoped full-coverage token (too broad), (C) multiple fine-grained tokens (extra setup). | VALIDATED: fine-grained PATs are single-owner and cannot access outside-collaborator / other-users' repos; classic `repo` covers all-accessible but is coarse. Least-privilege chosen over full coverage. |
| **D4 / D15** | Repo↔project scope & mapping | **Default = commit/LOC across all GitHub repos the authed identity contributed to in-window (discovery via `contributionsCollection`); tickets across all the user's Praxis spaces.** How a Praxis project maps to specific git repos (for a stricter "only connected projects' repos" scope) is an **open mapping question** — no such mapping exists today. | Grounding: no repo↔space link exists; "no extra setup" rules out manual mapping. |
| **D5** | Default-branch-only / forks-excluded LOC caveat | **Accept, disclosed in a chart footnote.** | Inherent to GitHub aggregates. |
| **D6** | GitLab | **Deferred to phase-2** (own token, `commit_count:0` bulk-push quirk). GitHub only v1. | Grounding. |
| **D7** | Cache TTL / "fresh" | **Default 60–120s server TTL + manual Refresh + "last updated"; longer TTL (e.g. 10–30m) for 12-month/all-time ranges.** | GraphQL has no conditional requests; TTL + coarse buckets are the levers. |
| **D8** | Historical LOC backfill | **Feasible from git history; fetch per-range on demand, cache aggressively.** | Grounding: `history(since,until)` returns additions/deletions historically. |
| **D9** | Timezone for day buckets | **Default = user's browser-local day boundary, computed consistently, tz disclosed.** | Avoids "reset at a weird hour." |
| **D10** | Commit identity resolution | **Default = `author.user.login` == the key-owner's login; fallback verified-email match; disclose unattributed count.** Since the key is Matt's own account, "mine" = the key owner. | Grounding. |
| **D14** | Series definitions | **S1 added, S2 deleted, S3 net = added−deleted, S4 tickets/day** (covers all listed phrasings). | User list; disambiguated. |
| **D16** | Range-selection persistence | **Default = persist within session (and remember last choice); resets to last-4-weeks default otherwise.** | Low regret. |
| **D17** | Bucketing per range | **Default = daily for ≤4 weeks; weekly for 12 months; monthly for all-time.** | Legibility + node limits. Flag if per-day is wanted throughout. |
| **D18** | Series show/hide toggles | **Default = all four shown, user can toggle.** | Low regret. |
| **D19** | Chart library | **Default = a lightweight React chart lib (or minimal SVG), self-contained; no heavy dep.** Confirm at plan/eng-review. | Frontend is React/Vite; keep bundle lean. |
| **D20** | New backend route shape | **Default = `GET /productivity?range=<r>` returning bucketed S1–S4 series, next to `/requirements/completeness`, same tenancy deps; fans out to GitHub + Praxis, caches.** | Grounding: monolithic `create_app` `@app.get` pattern. |

### Adversarial challenges recorded (for af-intake-plan; not resolved here)
- **C1 (history gap):** S4 over "all time"/"12 months" is untruthful before finish-time instrumentation
  — is a forward-only S4 acceptable, or must we attempt reconstruction from episodes/outcomes? (D1)
- **C2 (permission contradiction):** user wants "no code reading" but LOC *requires* Contents:Read —
  the requirement as stated is not fully satisfiable; confirm the read-only-Contents floor is acceptable. (D3)
- **C3 (unbounded scope):** "every repo I contribute to" × "all time" × per-day is an unbounded query
  surface (node limits, rate limits, latency) — forces bucketing + caps + caching (D17/D7/edge). 
- **C4 (private-contribution visibility):** GitHub counts private contributions only if the account's
  "Private contributions" profile toggle is on, else opaque — depend on it, or need a fallback?
- **C5 (mapping gap):** "projects I connect to Praxis and contribute with" implies a repo↔project link
  that doesn't exist; without it, scope defaults to all-contributed-repos — is that the intended scope? (D4/D15)
- **C6 (single-key blast radius):** one backend-held personal token grants read to all Matt's private
  repos; confirm storage/encryption/rotation expectations.
- **C7 (silent truncation):** long-range node/rate caps could undercount — must surface, not silent.

---

## 6. Defaults taken (conventions, flagged)
- Tab wired via `ViewTab` union + `SectionTabs` button + `App.tsx` render branch (seam identified).
- Frontend calls the new route via `apiClient.ts` + `contractHeaders` (Cognito bearer + org/space headers).
- Backend route as a nested `@app.get` in `create_app` next to `/requirements/completeness`.
- GitHub access via server-side GraphQL (`history` for LOC time-series, `contributionsCollection` for
  active-repo discovery) — the ideation doc's fast path.

## 7. Rigor-mode log
- ce-brainstorm-equivalent scope pass ✓ · ce-ideate ✓ · research-to-ground ✓ · adversarial pass ✓
  (C1–C7) · edge-state enumeration ✓.
- **ce-doc-review panel ✓ (round 1, 2026-07-24):** 7 personas — coherence, feasibility, security,
  scope-guardian, adversarial, design, product. Findings integrated in §8 (fixes applied + new
  decisions D22–D33 + confirmations + strategic flags). No finding dropped.

---

## 8. ce-doc-review integration (round 1) — findings, fixes, new decisions

### 8a. Feasibility CONFIRMATIONS (validated against code — good news)
- **D1 confirmed at code level:** `release()` writes `build_state='finished'` with **no timestamp** and
  drops `claim_heartbeat_at`; `record_outcome` stamps no time; `facts` has `created_at` but **no
  `updated_at`** (`postgres_vector_graph.py:2341-2350`, `:1170-1195`, `migrations/0000_initial.sql`).
  No available timestamp reconstructs finish history → forward-only S4 is correct.
- **D20 route shape confirmed:** monolithic `create_app` `@app.get`, `/requirements/completeness`
  uses the exact deps (`app.py:2944`). **D19 confirmed:** no charting lib in `package.json`
  (`@xyflow/react` is a node-graph canvas, not series) → chart lib is a real new dep.
- **Tab change confirmed 3–4 files** + one named edit: the FilterBar hide is a hard-coded condition
  at `App.tsx:705` — must add `&& viewTab !== "productivity"`. Added to §6 defaults.

### 8b. Fixes APPLIED to earlier sections (supersede where noted)
- **F-fix-1 (D9 timezone — was unimplementable):** default changed to **browser sends its tz-offset as
  a query param AND the server cache is keyed by (range, tz-offset)**; DST handled by computing offset
  per-bucket. Alternative (fixed server tz, disclosed) noted. Supersedes D9's "browser-local" wording.
- **F-fix-2 (D2 secret storage):** token **MUST** be stored in **AWS Secrets Manager following the
  existing `OPENROUTER_API_KEY` CDK pattern** (`infra/`), never a plaintext App Runner env var.
- **F-fix-3 (D17 bucketing aggregation — was undefined):** each bucket **sums** its days' values
  (sum-per-bucket, not mean); the y-axis label + series units track the bucket ("lines / week", "/ month").
  Supersedes the bare "lines / day" label for coarse buckets.
- **F-fix-4 (B7/D7 Refresh semantics):** **Refresh force-fetches for ≤4-week ranges; for 12-month/
  all-time it re-uses the long-TTL cache unless a "force" affordance is used; all Refresh is
  debounced/rate-limited client+server** so a shared-key self-DoS is impossible.
- **F-fix-5 (D18 reclassified):** series show/hide toggles moved from ACCEPT → **DEFER** (inconsistent
  with the other deferred interactivity; a 4-line legend suffices). Removes the D16-persistence dependency.
- **F-fix-6 (feasibility note):** `contributionsCollection` has a **~1-year-per-query window** (and
  ~5-year reach) → 12-month/all-time discovery must **chunk year-by-year**, not a single call.

### 8c. NEW decisions (forks the panel forced — for af-intake-plan)
| # | Fork | Resolution (flagged) | Source |
|---|------|----------------------|--------|
| **D22** | **LOC-vs-ticket axis scale** (S1–S3 = hundreds–thousands; S4 = 0–5 → S4 invisible on one linear axis) | **Default = dual y-axis (left LOC, right tickets)**; alternatives: two stacked charts sharing the x-axis, or normalized/log scale. **Changes what's built.** | design (high, conf 100) |
| **D23** | **Cross-tenant git-data leak** — one backend-held token = Matt's identity, but `/productivity` is reachable by any authed tenant; `current_user`/`active_org` deps do NOT gate the *git* half | **Default = restrict the tab + `GET /productivity` git series to the token-owner identity (explicit user-id allowlist); any other tenant gets a hard 403 / feature-disabled, never a silent pass-through.** B8's "guarded by existing deps" is corrected. | security + adversarial (HIGH, cross-persona) |
| **D24** | **Token rotation / expiry / revocation / audit** (fine-grained PATs expire ≤1yr; C6 was unresolved) | **Promote C6:** define rotation cadence + owner, a revocation runbook, and audit-log of token *usage* (timestamp/endpoint/repo — never the token). | security (HIGH) |
| **D25** | **Token repo-scoping** (blast radius = all Matt's private repos) | **Default = scope the fine-grained token to a selected-repo allowlist** (the repos actually surfaced), not "all repos the account can read." | security (med-high) |
| **D26** | **Commit-attribution accuracy** (squash-merge attributes a whole PR to the merger; co-author trailers; rebase/force-push move or double-count LOC; default-branch-only) | **Default = accept + disclose an "accuracy floor" footnote**: "default-branch, squash-attributed LOC — not authorship." Flag if a stricter attribution is wanted. | adversarial (med-high) |
| **D27** | **S4 sequencing** — on the **default 4-week view**, S4 is a dead flat-zero line for the first ~month post-instrumentation | **Default = land finish-timestamp instrumentation NOW as a standalone change; grey/annotate S4 until its start-date is within the selected window; ship the full 4-series page once S4 has ~weeks of history.** (Also a product recommendation.) | adversarial + product + scope |
| **D28** | **Chart-scope honesty** — S1–S3 come from GitHub repos, S4 from Praxis spaces; they are *different sets*, so plotting them on one chart implies a correlation the data can't support | **Default = label the two scopes explicitly (git-activity vs Praxis-ticket-activity), NOT "this project's productivity"**; building a real repo↔space mapping is out of v1 (contradicts no-setup). | adversarial (high) + coherence/C5 |
| **D29** | **Bound "all time"** (unbounded; cold first load = guaranteed rate-limit, no cache to fall back on) | **Default = floor at account-creation OR last N years (whichever later), labeled; consider async/incremental build for all-time rather than synchronous-on-open.** | adversarial + scope (med-high) |
| **D30** | **First-run / zero-scope state** (new user, zero contributed repos + zero spaces) distinct from "empty window" | **Default = a dedicated first-run state ("nothing to show yet / connect a project"), not a flat zero-line.** | design (med) |
| **D31** | **Disclosure architecture** — ≥7 distinct caveat strings around one chart (branch/fork, tz, ticket-start-date, N-unattributed, rate-limited, showing-first-N, stale-age) | **Default = a single persistent footnote strip for static caveats + an "ⓘ" affordance for per-load conditions + inline caption near the S4 legend for its start-date.** | design (med) |

### 8d. STRATEGIC findings — flagged for the human (premise challenges; NOT resolved, NOT overridden)
The product-lens panel raised premise-level challenges that **contradict the feature as the user
directed it**. Per methodology these are recorded, not resolved away, and surfaced to the human:
- **D32 — State the goal.** The doc specifies *what/how* but never *why Matt looks at it / what he
  decides after*. Add one goal sentence before intake. **Provisional (flag):** "So Matt can see his
  own coding + ticket throughput over time at a glance." Override with the real objective.
- **D33 — "LOC = productivity" is a contested premise.** Net-lines (S3) rewards accretion and punishes
  refactors; LOC is activity, not value. Options: (a) keep but **label S1–S3 as "commit activity," not
  "productivity"**; (b) demote/drop S3 net; (c) make **commits/day + tickets/day the primary series,
  LOC secondary**. **User has explicitly asked for the LOC line graph — recorded for the human to
  confirm or reframe, not changed unilaterally.**
- **Strategic fit + sequencing (record):** product-lens argues a **Praxis-native** dashboard
  (facts admitted/rejected, contradictions resolved, tickets finished — all from Praxis's own store,
  no GitHub secret) is more identity-aligned and far cheaper, and that GitHub should be sequenced
  **last**. Recorded as an alternative for the human; the user's stated intent (git metrics) stands
  unless they choose to reframe.

### 8e. Coherence
Doc judged internally consistent on all 5 tension points checked (S4 semantics, S1–S3 mapping, range-
vs-bucket terms, scope layering, "no *user* setup"); no coherence fixes required.
