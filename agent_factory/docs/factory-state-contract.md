# Factory State Contract

The canonical contract for the agent-factory refactor. Every other agent in this refactor reads
THIS document before touching ticket/check state. It defines the meta keys, the per-ticket
lifecycle, the check-resolution-is-a-query rule, the fail-closed rule, the auth/env surface, and the
public function signatures of `hooks/_praxis.py` and `hooks/_ticket_state.py`.

## Single source of dynamic truth

Praxis is the ONLY store of dynamic build/validation state. It holds tickets (requirements), checks,
and the outcomes/state that say what is built and what passed. Plugin code is deterministic plumbing
that reads Praxis live.

- **JSON is STATIC CONFIG ONLY.** No `json.dump` of build/validation/review/audit/preflight state.
  The `.factory/*.json` manifest pattern is being purged. These modules write NO local state files.
- **Two-tier validation.** A **validation REQUIREMENT** (`category="check"`) is an abstract
  *"what must be proven"* fact — declarative, read-only during builds, owning its own applicability
  predicate (`meta.applies_to` tag / bound surface). A **VALIDATION** is a concrete, executable
  instance the worker AUTHORS to faithfully COVER the resolved requirements (a `run` command whose
  exit code is the signal, declaring the requirement ids it `covers`). A ticket carries identity
  (tags, surfaces, semantics) but NEVER an authored requirement list.
- **Which requirements apply is a QUERY**, resolved fresh at ticket start (tag union surface against
  active requirements). Never pre-bound onto the ticket. The worker then synthesizes covering
  validations; a ticket is done iff coverage is complete AND every validation passes.
- **A build run is a WHOLE-SET, scope-bearing commitment.** At run start af-build stamps a
  `run_owner`/`run_at` marker on every in-scope incomplete ticket; the single Stop gate enforces the
  entire marked set until each is `finished` (or terminally `blocked`), closing the between-ticket
  window. A non-coverable / credential-only ticket is `blocked` (surfaced for owner action, excluded
  from churn), never a silent forever-deadlock.

## Fail-closed rule

Praxis is a HARD dependency. If it is unreachable / unauthenticated / errors, `_praxis` raises
`PraxisUnreachable`. A Stop-gate that catches `PraxisUnreachable` MUST BLOCK — it may never fail
open. A gate that cannot prove the truth does not let work pass.

## Auth / environment

The client (`hooks/_praxis.py`) is stdlib-only (`urllib`, `json`) so a bare hook subprocess can use
it — no `httpx`, `pycognito`, or `praxis` import.

| Env var               | Meaning                                                                 |
|-----------------------|-------------------------------------------------------------------------|
| `PRAXIS_API_BASE_URL` | Base URL. Default `http://localhost:8000`.                              |
| `PRAXIS_API_KEY`      | Preferred auth. Sent as `x-praxis-key`.                                 |
| `PRAXIS_ORG`          | Tenant org, sent as `x-praxis-org`. Default `agent-factory`.            |
| `FACTORY_PROJECT`     | Praxis project name → `prd-<project>`. Falls back to the cwd basename. **REQUIRED when the repo dir name differs from the project name.** |
| `PRAXIS_AUTH_DISABLED`| `1` = dev seam; skip auth entirely (server has a matching seam).        |
| `COGNITO_CLIENT_ID`   | Used to mint a bearer when no API key is set.                           |
| `COGNITO_REGION`      | Cognito region for the mint. Default `us-east-1`.                       |

All of the above (including `FACTORY_PROJECT`) may be set as real shell env vars **or** in the factory
`<repo>/.env` — a bare Stop-hook subprocess does not inherit a shell-sourced `.env`, so `hooks/_praxis.py`
loads it explicitly (real env vars always win). Set `FACTORY_PROJECT` whenever the checkout directory
name is not the Praxis project name (e.g. a repo cloned as `bestie-api` that builds the
`google-shopping-scraper` project) — otherwise the build-completeness gate resolves the wrong
`prd-<project>`, finds no run marker, and silently goes inert (the whole-set completeness backstop is lost).

**Auth resolution order:** `PRAXIS_AUTH_DISABLED=1` → no auth header · else `PRAXIS_API_KEY` →
`x-praxis-key` · else mint a Cognito ID token from `~/.praxis/mcp.json`'s `refresh_token` via a raw
`InitiateAuth` REFRESH_TOKEN_AUTH call (minimal replication of `knowledge/mcp/identity.py:token()`,
without importing praxis) → `Authorization: Bearer`. If no credential is available, **fail closed.**

There is **no space header for working memory.** Working-memory reads/writes resolve to `(org,
authenticated principal)` — the client sends `x-praxis-org` and the auth credential only. `space` +
`snapshot` are emitted (as `x-praxis-space` + `x-praxis-snapshot`) ONLY on the explicit snapshot-bound
reads/writes below (the checks-snapshot seam and the mutable `prd-<project>` ticket ops).

## Canonical meta keys (on the requirement / ticket node)

| Key                    | Type                              | Meaning                                                        |
|------------------------|-----------------------------------|----------------------------------------------------------------|
| `build_state`          | `"incomplete"｜"in_progress"｜"finished"｜"blocked"` | Lifecycle state. Absent ≡ `incomplete`.    |
| `depends_on`           | `list[str]`                       | Prerequisite ticket ids (fact id or requirement id) that must be `finished` before this ticket is claimable. |
| `block_reason`         | `str`                             | Why a ticket is `blocked` (surfaced; needs owner action).      |
| `claim_owner`          | `str`                             | Session/agent id holding the lease.                            |
| `claim_at`             | `float` (epoch seconds)           | When this owner first claimed.                                 |
| `claim_heartbeat_at`   | `float` (epoch seconds)           | Last liveness bump.                                            |
| `claim_lease_ttl`      | `int` (seconds)                   | Lease is STALE when `now - claim_heartbeat_at > claim_lease_ttl`. |
| `required_validations` | `list[str]`                       | Resolved requirement ids — THIS pass's coverage contract.      |
| `pinned_checks`        | `list[{validation_id, covers, run, passed, ran_at}]` | The synthesized VALIDATIONS — the eval.     |
| `run_owner`            | `str`                             | Session id of the active whole-set build run this ticket is in. |
| `run_at`               | `float` (epoch seconds)           | Run-marker heartbeat; run is STALE when `now - run_at > DEFAULT_RUN_TTL_S`. |
| `run_scope`            | `str`                             | Human label of the run's scope (for the gate's report).        |

`pinned_checks` entry: `{ "validation_id": str, "covers": list[str], "run": str,
"passed": bool｜null, "ran_at": float｜null }` (null = not yet run). The key name is retained for
back-compat with the Praxis server's `claim` view and the eval harness, but entries now describe
synthesized VALIDATIONS, not raw checks.

A **GRADED** validation (a min-of-axes rubric check, see
`docs/plans/2026-07-21-001-feat-graded-rubric-checks-plan.md`) carries two extra keys and is
otherwise identical: `"kind": "graded"` and `"rubric": {axes, confidence_floor, criterion,
judge_prompt}` (the rubric FROZEN at synthesis time — VERIFY reads this copy, never the live
seeded library). After it runs it also stashes `"verdict": {code_hash, passed, min_axis, reason,
axis_scores, defects}` — the content-hash cache so identical code is never re-graded. **The gate
(`all_validations_passed`) reads only `passed`**, so every graded extra is inert to it: a graded
check's subjective verdict reduces to the same boolean a binary check produces. Binary validations
omit `kind`/`rubric`/`verdict` and stay byte-compatible with the shape above. Per-ticket graded
loop state lives in `graded_loop: {validation_id: {iters, last_defects, last_hash}}` (iteration cap
+ defect-count monotonicity guards; cap → `block()`). `build_state`/`claim_owner`/`claim_heartbeat_at`/`lease_live`
align with the server's `claim` view, so `/requirements/incomplete` and these client writes agree.

## Per-ticket lifecycle

0. **open the run** — resolve scope → in-scope incomplete ticket ids; `stamp_run(cids, owner, scope)`
   marks the whole set so the gate enforces it as one run. `refresh_run` at each ticket boundary;
   `clear_run` at run end.
1. **find + start (ONE at a time)** — pop the SINGLE dependency-ready front via
   `next_ready_ticket(incomplete)` (not finished, not blocked, every `depends_on` prerequisite finished);
   then `claim` (incomplete → in_progress, stamp lease); then `resolve_validation_requirements` (the QUERY);
   then `pin_requirements` which TRUNCATES any prior validations and writes the resolved requirement ids as
   the coverage contract (`required_validations`). (`start_ticket` does claim + resolve + pin.) The worker
   ships this one ticket end-to-end before it looks at another — the run marker + gate guarantee the rest of
   the scope gets done; attention stays on one ticket.
2. **synthesize + build** — author concrete validations that faithfully COVER every requirement
   (`pin_validations`, entries declaring `covers` + `run`); `coverage_gap` must be empty. Then do the
   work to satisfy the acceptance condition. `heartbeat` periodically to keep the lease + run marker live.
3. **verify** — run each pinned validation; record each pass ON THE TICKET NODE via
   `record_validation_pass` (never on the requirement fact).
4. **finished IFF** `all_validations_passed` (coverage complete, ≥1 validation, all passed) →
   `release(state="finished")` (also clears this ticket's run marker). Yielding cleanly →
   `release(state="incomplete")` (run marker KEPT — the gate keeps it in scope). A non-coverable /
   credential-only / unsatisfiable ticket → `block(state="blocked")` (surfaced, excluded from churn).

**Doneness is THE EVAL, not a count.** A ticket is done iff coverage is complete and its synthesized
validations (the eval) all pass, recorded as the hard enum `build_state="finished"` — the single
authoritative completion signal. The `record_outcome` success/failure **count** is a trust/utility
weighting only and is **never** the doneness criterion; nothing in the factory may read a success count
as "done." The one Stop gate honors `build_state="finished"`/`"blocked"` directly (it skips/surfaces
them even if a count-derived list still lists them).

**Claiming is a LEASE, not a lock.** A stale lease (heartbeat older than ttl) is auto-reclaimable so
nothing dangles. **"A build run is active"** ≡ this session owns a live `in_progress` claim **OR** a
non-stale whole-set `run_owner` marker scopes work to it — read from Praxis, NOT a local file flag. The
run marker is what lets the gate enforce the *whole scoped set*, not just a currently-held ticket.

**Race-tolerance (v1).** `claim` is a read-modify-write over `patch_meta` (PATCH `/candidates/{cid}`,
which MERGES meta). No server-side CAS is assumed. Two agents can both claim a free/stale ticket — a
rare, HARMLESS double-claim (idempotent wasted work), not corruption.

**Note on key deletion.** `patch_meta` MERGES (it cannot delete keys), so `release` NULLs the lease
keys rather than removing them; `_lease_live` treats null heartbeat/ttl as not-live.

## Requirement resolution is a query (tag union surface)

`resolve_validation_requirements(ticket, project, scope=None)`. The `scope`
arg is the ONE seam between the two callers — everything downstream (pin / coverage / pass) is identical:

- **`scope="validation"`** (af-build PER-TICKET; `start_ticket` passes this) — the **MANDATORY (precise)**
  coverage contract: the de-duplicated union of three lanes, then filtered to validation-scope:
  - **tag match** — active `category="check"` facts whose `meta.applies_to` contains any of the ticket's
    tags (`meta.tags` / `meta.applies_to`); via `facts_by`.
  - **`"*"` wildcard** — universal gates (typecheck/build/lint/test) that apply to EVERY ticket, pulled
    with an explicit `facts_by(meta={"applies_to": "*"})`. This is a SEPARATE pull because a per-tag query
    can never surface a `["*"]` check (array-membership matches the STORED value, and a ticket's concrete
    tags never include the literal `"*"`) — without it the baseline floor silently fails to resolve.
  - **surface match** — checks bound (via the `renders` edge) to any surface the ticket renders; via
    `/surfaces/{screen}/checks`. A UI check is surface-bound (or UI-tagged) so it resolves ONLY onto
    screen-rendering tickets — never onto a backend-only ticket.
- **`scope="planning"`** (af-intake-plan WHOLE-PLAN gate, B3) — planning lenses are GLOBAL considerations
  (`applies_when`, NOT tag/surface-bound), so this returns the ENTIRE active `scope="planning"` checklist
  regardless of the subject's tags/surfaces. `ticket` is the plan-anchor the coverage contract hangs on.
  af-intake-plan then runs the SAME two-tier pass (`pin_requirements` → synthesize + `pin_validations` →
  `coverage_gap` empty + `all_validations_passed`) over the plan's Praxis facts — making lens-coverage a
  hard gate, identical in shape to the build-side validation and to the eval's depth scorer.
- **`scope=None`** (default, back-compat) — tag union surface across all check scopes.

**The semantic lane is ADVISORY, not part of the mandatory contract.** `retrieve_advisory_checks(ticket,
project, scope, checks_ref, top_k)` runs a hybrid retrieval (`/context`) of `category="check"` facts
close to the ticket's text and returns them as **candidate inspiration** for the worker's synthesis step.
They are NEVER pinned as `required_validations` and NEVER gate completion — the worker folds relevant ones
into its authored validations and ignores the rest. Keeping semantics OUT of the hard gate is deliberate:
a fuzzy retrieval that is irrelevant costs nothing, while a precise tag/wildcard/surface match is always
enforced. Per-ticket flow: **clear → resolve MANDATORY (precise) → retrieve ADVISORY (semantic) → LLM
authors validations covering the mandatory set (+ any advisory it honors) → build → every pinned
validation must pass (external signal) before `finished`.**

**The checks-space seam.** Check *reads* target a snapshot **separate** from the `prd-<project>`
ticket/plan snapshot, via a `checks_ref` parameter (`resolve_validation_requirements` /
`start_ticket`) that becomes a per-request `x-praxis-space` + `x-praxis-snapshot` override on
`facts_by` / `surface_checks` only — ticket state (claims, pins, passes) never moves off the
`prd-<project>` snapshot. Both snapshots live in the SAME project space (`space=<project>`, the bare
project name); only the snapshot differs. Default read snapshot by scope: `scope="validation"` →
**`building-validation`** (renamed from `coding-validation`), `scope="planning"` →
**`planning-validation`**, `scope=None` → the ticket/default reference. The `af-build` / `af-intake-plan`
slash argument `--checks-space=<...>` overrides per run as a `(space, snapshot)` pair
(`checks_ref=(space, snapshot)`); `checks_ref=None` forces the ticket/default reference. A check is
only resolvable if it was authored INTO the snapshot RESOLVE reads.

## Coverage is the contract; validations are the eval

The resolved requirements are PINNED as `required_validations`. The worker then synthesizes concrete
validations (`pin_validations`), each declaring the requirement ids it `covers`. `coverage_gap(ticket)`
returns the requirement ids not yet covered by any validation — it MUST be empty for the ticket to
finish. `all_validations_passed` is the single doneness predicate: coverage complete, ≥1 validation, all
passed. A requirement that cannot be turned into a runnable validation is a `block`, not a fake pass.

**The acceptance-condition floor (no-empty-contract guarantee).** `start_ticket` composes the contract via
`contract_with_floor`: the resolved checks PLUS the ticket's own binary acceptance condition as a synthetic
`<cid>::acceptance` requirement. So even when ZERO Praxis checks match, the contract is non-empty and the
worker has exactly one always-authorable target — the red→green acceptance test — which alone lets the
ticket finish. This closes the deadlock where "the validation step produced no evals" left a ticket that
could be neither finished (`all_validations_passed` needs ≥1) nor escaped. Only a ticket with NO checks AND
no acceptance condition yields an empty contract (a planning defect) → `block()`, never a silent wedge.

## Public API — `hooks/_praxis.py`

```python
class PraxisUnreachable(RuntimeError): ...   # fail-closed signal; callers BLOCK

incomplete_requirements(project: str, *, exclude_leased: bool = False, space: str|None = None, snapshot: str|None = None) -> list[dict]
# Pass the BARE project name. The endpoint prepends "prd-" itself; this fn strips a single leading
# "prd-" so an already-prefixed "prd-team-app" can never become "prd-prd-team-app" (→ empty → a
# gate that fails OPEN). Both "team-app" and "prd-team-app" resolve to bare "team-app".
# The prd-<project> tickets are a MUTABLE snapshot at (space=<project>, snapshot=prd-<project>);
# thread that (space, snapshot) reference so ticket reads/writes hit the project snapshot.
get_fact(cid: str, *, space: str|None = None, snapshot: str|None = None) -> dict   # full fact incl meta
facts_by(category: str|None = None, meta: dict|None = None, state: str = "active", space: str|None = None, snapshot: str|None = None) -> list[dict]
patch_meta(cid: str, meta_dict: dict, *, space: str|None = None, snapshot: str|None = None) -> dict   # MERGE meta (build_state/claim/pinned_checks)
record_outcome(cid: str, success: bool, *, space: str|None = None, snapshot: str|None = None) -> dict
surface_checks(project: str, screen_id: str, scope: str|None = None, space: str|None = None, snapshot: str|None = None) -> list[dict]
    # (space, snapshot)= override x-praxis-space + x-praxis-snapshot for this read only (the checks-snapshot seam)
context(query: str, *, top_k: int = 10, as_of=None, space: str|None = None, snapshot: str|None = None) -> list[dict]  # hybrid retrieval (semantic lane)
ping() -> bool                                              # smoke-test liveness (no snapshot)
```

Every method raises `PraxisUnreachable` on any connection/HTTP/auth error. A method emits
`x-praxis-space` + `x-praxis-snapshot` ONLY when BOTH `space` and `snapshot` are given; a required
`(space, snapshot)` reference that is missing (or defaults to empty) must RAISE — never silently fall
back to working memory, since a mis-routed checks read returning empty would fail a Stop gate OPEN.

## Public API — `hooks/_ticket_state.py`

```python
# canonical meta-key constants
M_BUILD_STATE, M_BLOCK_REASON, M_CLAIM_OWNER, M_CLAIM_AT, M_CLAIM_HEARTBEAT_AT, M_CLAIM_LEASE_TTL,
M_REQUIRED_VALIDATIONS, M_PINNED_CHECKS, M_RUN_OWNER, M_RUN_AT, M_RUN_SCOPE
DEFAULT_LEASE_TTL_S = 900     # per-ticket claim lease
DEFAULT_RUN_TTL_S   = 3600    # whole-set run marker (refreshed at each ticket boundary)

# --- requirements (the QUERY) + the coverage contract ---
resolve_validation_requirements(ticket, project="", scope=None, checks_ref=<default>) -> list[dict]
    # scope="validation" (af-build, per-ticket tag∪surface) | "planning" (af-intake-plan, whole checklist) | None
    # checks_ref= the (space, snapshot) seam: unset -> space=project + per-scope default snapshot
    #   (validation->building-validation, planning->planning-validation); a (space, snapshot) pair (or a bare
    #   snapshot name, space defaulting to project) overrides (--checks-space slash arg);
    #   None forces the ticket/default reference (space=project, snapshot=prd-<project>)
    # MANDATORY (precise) lanes only: tag ∪ "*" wildcard ∪ surface — the coverage contract
retrieve_advisory_checks(ticket, project="", scope=None, checks_ref=<default>, top_k=10) -> list[dict]
    # the SEMANTIC lane — advisory candidate checks (inspiration); NEVER pinned/required, never gates
default_checks_snapshot(scope) -> str|None                   # per-scope default read SNAPSHOT
    # validation -> "building-validation", planning -> "planning-validation", else None; the space half is project
DEFAULT_VALIDATION_CHECKS_SNAPSHOT = "building-validation"
DEFAULT_PLANNING_CHECKS_SNAPSHOT   = "planning-validation"
acceptance_requirement(cid, acceptance_text) -> dict         # the <cid>::acceptance floor requirement
contract_with_floor(cid, acceptance_text, resolved: list) -> list  # resolved checks + acceptance floor (dedup)
pin_requirements(cid: str, requirements: list) -> dict       # truncate validations + pin coverage contract

# --- worker-synthesized validations (the eval) ---
pin_validations(cid: str, validations: list) -> dict         # entries: {validation_id, covers:[id], run}
record_validation_pass(cid, validation_id, passed, ran_at=None) -> dict
coverage_gap(ticket) -> list[str]                            # requirement ids not yet covered ([] == ok)
all_validations_passed(ticket) -> bool                       # coverage complete + ≥1 + all passed

# --- dependency readiness (the FIND queue front) ---
deps_of(ticket) -> list[str]                                 # this ticket's depends_on prerequisite ids
unfinished_ids(items: list[dict]) -> set[str]               # ids of every not-finished ticket in a set
is_ready(item: dict, unfinished: set[str]) -> bool          # no depends_on still unfinished
pending_deps(item: dict, unfinished: set[str]) -> list[str] # which prerequisites are still unfinished
ready_tickets(items: list[dict]) -> list[dict]              # claimable NOW: not finished/blocked + ready
next_ready_ticket(items: list[dict]) -> dict|None          # pop the SINGLE queue front (one-at-a-time)

# --- claim / lease / lifecycle ---
claim(cid, owner, ttl=900) -> bool                           # incomplete -> in_progress (race-tolerant)
heartbeat(cid, owner) -> bool                                # bump lease (+ run marker) iff still held
release(cid, owner, state) -> bool                           # state in {"finished","incomplete"}
block(cid, owner, reason) -> bool                            # -> build_state="blocked" (surfaced, no churn)

# --- whole-set run marker (scope-bearing arming signal) ---
stamp_run(cids: list[str], owner, scope="all") -> int        # mark in-scope incomplete tickets at run start
refresh_run(cids: list[str], owner) -> int                   # bump run_at at each ticket boundary
clear_run(cids: list[str], owner) -> int                     # end the run (scoped set done / aborted)
run_live(meta: dict, now=None) -> bool                       # non-stale run marker present?

start_ticket(cid, owner, project="", ttl=900, checks_ref=<default>) -> list[dict]|None  # claim + resolve (space=project, snapshot from checks_ref) + pin
```

`ticket` arguments accept either a fact id (`str`) or an already-fetched fact (`dict`). All Praxis
errors propagate as `PraxisUnreachable` (fail-closed).
