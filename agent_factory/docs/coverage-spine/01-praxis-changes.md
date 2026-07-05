# Praxis changes needed

> Companion to [`00-overview.md`](00-overview.md). Short answer: **almost none to start.**

The earlier Praxis codebase map (of `../praxis`) established that `category`, `source`,
`meta`, and `scope` are **free-form** — only `requirement`, `surface`, `episodic` are
reserved. The factory's "skill resolves the applicable checks by query and PINS them onto the ticket
node (`meta.pinned_checks`); the gate reads the ticket's pinned checks live and enforces closure"
pattern keeps the check content in Praxis and writes NO files. So the new vocabulary needs **no Praxis
schema/code change**.

## The new fact: a "check"
```
category = "check"
meta.scope      = "planning" | "validation"          # which gate enforces it
meta.applies_to = "*" | surface-id | requirement-id/class | tech-tag   # applicability
meta.kind       = "deterministic" | "agent-evaluated"
meta.criterion  = "auth endpoints must reject expired tokens"          # the check text
meta.severity   = "high" | "med" | "low"
```
Stored in two snapshots: `planning` and `validation`, mounted read-only by the respective
skills (same pattern as today's `constitution` / `general-pool` mounts).

## Supported today — NO change
| Capability | Mechanism that already exists |
|---|---|
| Store checks as facts | free-form `category` + `meta` (writer-set meta wins) |
| Two read-only checklist snapshots | `save_snapshot` / `mount_snapshot` |
| Validation-fail → regress a ticket so the coder re-picks it | `record_outcome("failed")` → requirement re-enters `incomplete_requirements` as `regressed` |
| Bind a check to a surface/requirement | `meta.applies_to` + client filter, **or** the `renders`-edge binding (`bind_surface`) |
| Dedup / conflict as checks accumulate | `on_conflict="surface"` + the contradiction machinery |
| Coverage scoring for the **eval** | none — the golden is a checked-in file; `coverage.py` scores client-side |

## The one real enhancement — a thorough per-part retrieval query (NEEDED, not optional)
**A targeted retrieval query**, e.g. `related_to(part, scope)` /
`checks_for_surface(project, screen_id, scope)` / a generic `facts_by(category, meta_filter)`.

Originally scoped as an optional optimization. The coverage engine
([`05-coverage-engine.md`](05-coverage-engine.md)) **promotes it to needed**: its completeness
rests on pulling *everything related to a part* (exhaustive-for-that-part), and semantic
`get_context` is **top-k** — it samples, it doesn't enumerate, so it can silently drop a
related insight. Over thousands of insights, `list_graph` + client filter is also untenable on
token cost. A complete, structured `related-to(part)` enumeration is what makes "thorough"
real and is the sole remaining defense against a silently-missed insight (G1).

## Verify in `../praxis` before relying on these (load-bearing specifics)
1. Does `incomplete_requirements(project)` filter `category == "requirement"` exactly? — so
   `category="check"` facts stay OUT of that query and checks are tracked via the requirement
   they're bound to, not as phantom incompletes.
2. Will the structural **contradiction detector** fire between a claim-shaped current-state
   fact and a goal requirement? — and conversely, ensure two *checks* don't auto-merge
   (use distinct phrasing / raw insert if they do).
3. Can `get_context` retrieve checks reliably by scope, or is `list_graph` + filter required
   (i.e. is the optional retrieval query actually needed sooner)?

## Recommendation
Build everything against **today's Praxis**. Treat the checks-retrieval query as a
fast-follow. Do the 3 verifications above before locking the validation-gate design.
