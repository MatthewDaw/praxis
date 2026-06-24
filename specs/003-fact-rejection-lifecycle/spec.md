# Feature Specification: REJECTED state + retained-contradiction lifecycle

**Feature Branch**: `003-fact-rejection-lifecycle`

**Created**: 2026-06-23

**Status**: Draft

**Input**: User description: "docs\proposals\2026-06-23-fact-rejection-contradiction-lifecycle.md"

## Clarifications

### Session 2026-06-23

- Q: For the `decayed` → `rejected` rename, should we build a backward-compatibility shim or just migrate the data? → A: One-shot, idempotent data update only; no backward-compat shim (no production rows to preserve).
- Q: Does a fact's contradiction view include unresolved (pending) conflicts and let the reviewer resolve them in place? → A: Yes — the per-fact view shows both pending and resolved contradictions and the per-state action resolves a pending one in place. Additionally, a separate global view lists all pending contradictions (those where one side is still pending rather than rejected).
- Q: How is the "never both active" invariant protected against concurrent resolution actions? → A: Serialize each resolution atomically and re-check/enforce the invariant at write time (last action wins safely); no user-facing concurrency-conflict UX.

### Re-evaluation 2026-06-23 (post single-facts-spine refactor)

The spec was reconciled against commit `dbf60d9` ("collapse knowledge graph onto a single facts spine"), which landed after the original draft. Behavioral requirements (User Stories, FRs, Success Criteria) are unchanged and remain valid. Updated only the Assumptions/Out-of-Scope to reflect: the deleted `candidates` store, the Postgres-only server, the already-facts-backed dashboard, the now-active `fact_edges`, the `migrations/` convention, and the decision to **extend existing `/candidates` + `/contradictions` routes** rather than add a parallel `/facts…` API. `plan.md`, `research.md`, `data-model.md`, and `contracts/` must be regenerated against this baseline before task generation.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Approving a correction preserves the fact it replaces (Priority: P1)

When a newly approved fact contradicts a fact that is already live, the system keeps **both** facts. The newly approved fact becomes live; the fact it contradicts is moved to a retired ("rejected") state with its original wording intact, and the two are linked so the contradiction can be reviewed and reversed later. Today the losing fact's text is destroyed; this story stops that loss.

**Why this priority**: This is the substantive, highest-value change. It removes silent, irreversible destruction of knowledge — the prior wording is preserved for audit and the resolution can be undone. Every other story builds on this retained relationship.

**Independent Test**: Approve a fact B that conflicts with an already-live fact A. Verify A still exists with its original text, A is now in the rejected state, B is live, and the two are linked as a resolved contradiction. Confirm no fact's text was overwritten.

**Acceptance Scenarios**:

1. **Given** a live fact A and a new fact B that contradicts it, **When** B is approved, **Then** B becomes live, A becomes rejected with its original text intact, and A and B are linked as a resolved contradiction.
2. **Given** a fact B approved over several conflicting facts at once, **When** the approval is applied, **Then** every conflicting fact becomes rejected, each linked to B, and none is overwritten.
3. **Given** a contradicted fact that was only proposed (never live), **When** the opposing fact is approved, **Then** the proposed fact is moved to rejected (not silently dropped) and linked, preserving a uniform audit trail.
4. **Given** any two facts that contradict each other, **When** resolution completes, **Then** they are never both live at the same time.

---

### User Story 2 - Review facts by state and resolve contradictions from a fact (Priority: P2)

A reviewer can browse facts filtered by lifecycle state (proposed / active / rejected / all). Opening a fact shows the other facts it contradicts, each with its current state and a single context-appropriate action: reject a still-active or proposed contradictor, or re-approve a rejected one (which swaps the winner). When an action retires a fact that participates in *other* contradictions, the reviewer is told that fact may warrant a closer look.

**Why this priority**: This is the human control surface that makes the retained relationships (Story 1) reviewable and reversible. It delivers the day-to-day value but depends on Story 1's data being in place.

**Independent Test**: From the review surface, filter facts by each state and confirm the right facts appear. Open a fact with a contradiction, use the offered action, and confirm the states update and the relationship stays linked. Trigger a retirement of a fact that has another contradiction and confirm the review notice appears.

**Acceptance Scenarios**:

1. **Given** facts in different states, **When** the reviewer filters by a state, **Then** only facts in that state are listed, using the label "rejected" (never "decayed").
2. **Given** a fact with contradictions, **When** the reviewer opens it, **Then** each contradicted fact is shown with its state, and the action offered is "Reject" for active/proposed contradictors and "Approve" for rejected ones.
3. **Given** a rejected fact shown as a contradictor, **When** the reviewer approves it, **Then** it becomes live, the previously live fact becomes rejected, the pair stays linked, and the change is reflected without a manual refresh.
4. **Given** an action retires a fact that has one or more *other* contradiction relationships, **When** the action completes, **Then** the reviewer sees a notice identifying that fact for review and linking to it.
5. **Given** an action retires a fact that has no other contradictions, **When** the action completes, **Then** no review notice is shown.
6. **Given** several unresolved (pending) contradictions exist, **When** the reviewer opens the global pending-contradictions view, **Then** every contradiction with at least one still-pending side is listed; resolving one removes it from the list.

---

### User Story 3 - Delete facts safely, with live facts protected (Priority: P3)

A reviewer can permanently delete a fact only when it is proposed (never went live) or rejected (already retired). Attempting to delete a live fact is refused and the reviewer is directed to reject it first. Deleting a fact also removes its contradiction links, so it disappears from other facts' contradiction lists.

**Why this priority**: Cleanup of retired/never-live facts. It is the lowest-risk, lowest-frequency operation and depends on the rejected state and links established by the earlier stories.

**Independent Test**: Attempt to delete a live fact and confirm refusal with guidance to reject first. Reject it, then delete it and confirm it is gone along with its links. Delete a proposed fact directly and confirm success.

**Acceptance Scenarios**:

1. **Given** a live fact, **When** a delete is attempted, **Then** it is refused with a message directing the user to reject the fact first.
2. **Given** a rejected or proposed fact, **When** it is deleted, **Then** the fact is removed permanently.
3. **Given** a deleted fact that was linked to another fact's contradictions, **When** the deletion completes, **Then** the deleted fact no longer appears in any other fact's contradiction list and the remaining fact is otherwise unaffected.

---

### Edge Cases

- **Re-approving a former loser**: approving a rejected fact flips it to live and demotes the former winner to rejected; the pair stays linked with only states/direction changed. The demoted fact is then subject to the "other contradictions" review notice.
- **Multi-way contradictions**: one approval may retire several facts; each retired fact is independently linked and independently evaluated for the review notice.
- **Legacy retired facts**: facts retired under the old "decayed" terminology must read back as "rejected" after the change, with no loss of data.
- **Ripple scope**: the contradiction that *caused* a retirement does not itself count as an "other contradiction" — the notice fires only when a separate contradiction relationship exists.
- **No auto-cascade**: retiring a fact never automatically resurrects, resolves, or re-rejects any further fact; the reviewer always decides.
- **Concurrent resolutions**: when two resolution actions touch the same fact or pair at nearly the same time, each is applied atomically and the invariant is re-checked at write time, so the last action wins without ever leaving two contradicting facts both active.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The fact lifecycle states MUST be **proposed**, **active**, and **rejected**. The retirement state previously named "decayed" MUST be renamed to "rejected" everywhere it is surfaced (data, labels, filters, evaluation cases).
- **FR-002**: Existing facts recorded in the prior "decayed" state MUST be readable as "rejected" after the change, with no loss of their stored content.
- **FR-003**: When a fact is approved and it contradicts one or more existing facts, the system MUST keep the contradicted facts (preserving their original content), move each to the rejected state, and keep the approved fact active. It MUST NOT overwrite or destroy any contradicted fact's content.
- **FR-004**: Each resolved contradiction MUST record a bidirectional link between the two facts so the relationship is discoverable from either fact and can be reviewed and reversed.
- **FR-005**: The system MUST enforce that two facts which contradict each other are never both active at the same time. Resolution actions MUST be applied atomically with the invariant re-checked at write time, so concurrent actions cannot violate it (last action wins).
- **FR-006**: A contradicted fact that was only proposed (never live) MUST also be moved to rejected and linked, rather than silently dropped.
- **FR-007**: The system MUST distinguish a **pending/unresolved** contradiction (two facts conflict, no winner chosen) from a **resolved** contradiction (a winner was approved, the loser rejected).
- **FR-008**: When an approval or rejection moves a fact to rejected, the system MUST report whether that fact participates in any contradiction relationship *other than* the one that caused this retirement.
- **FR-009**: Retiring a fact MUST NOT automatically change the state of any other fact beyond the direct loser of the current action (no auto-cascade, auto-resolve, or auto-resurrect).
- **FR-010**: Re-approving a rejected fact MUST flip it to active and move its currently-active contradictor to rejected, keeping the pair linked and only changing states/direction.
- **FR-011**: Users MUST be able to list facts filtered by lifecycle state (proposed, active, rejected, and all).
- **FR-012**: Users MUST be able to view, for a given fact, the facts it contradicts — both unresolved (pending) and resolved — each annotated with its current state.
- **FR-013**: For a contradicted fact shown in review, the system MUST offer exactly one action determined by state: reject an active or proposed contradictor, or approve a rejected contradictor. Acting on a pending (unresolved) contradiction from this view MUST resolve it in place.
- **FR-013a**: The system MUST provide a separate global view that lists all pending contradictions — those where one of the two facts is still pending (not yet rejected) — so unresolved conflicts can be found without opening each fact.
- **FR-014**: Deletion of a fact MUST be permitted only when the fact is proposed or rejected. A delete targeting an active fact MUST be refused with guidance to reject it first.
- **FR-015**: Deleting a fact MUST remove all of its contradiction links so it no longer appears in any other fact's contradiction list, leaving the linked facts otherwise unchanged.
- **FR-016**: Reject and delete MUST remain separate operations: reject is a reversible state change that keeps the fact and its links; delete is irreversible removal.
- **FR-017**: The review surface MUST reflect state and link changes resulting from an action without requiring a manual page refresh.
- **FR-018**: Where a transient contradiction marker and the stored contradiction relationship would otherwise diverge, the stored relationship MUST be the single source of truth.

### Key Entities *(include if feature involves data)*

- **Fact**: A unit of knowledge with content, provenance, and a lifecycle state of proposed, active, or rejected. Active facts shape downstream reader output; proposed facts are staged; rejected facts are retired but preserved.
- **Contradiction relationship**: A link between two facts that conflict. It carries a status of pending (no winner chosen) or resolved (a winner is active, the loser is rejected), and is discoverable from either fact. Removing a fact removes its relationships.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of approvals that contradict an existing fact preserve the contradicted fact's original content (zero content destroyed), verified by a regression check against the prior overwrite behavior.
- **SC-002**: At no point can a reviewer find two mutually-contradicting facts both in the active state.
- **SC-003**: 100% of resolved contradictions are reversible — re-approving a rejected fact restores it to active and demotes its contradictor, with the relationship still intact.
- **SC-004**: A reviewer can find every fact in a chosen lifecycle state and, from any fact, see all facts it contradicts with correct state labels.
- **SC-005**: Deleting a fact removes it from 100% of other facts' contradiction lists; deletions of active facts are refused 100% of the time.
- **SC-006**: The term "decayed" no longer appears in any user-facing label, filter, or state value; all retired facts display as "rejected".
- **SC-007**: When a retirement affects a fact with other contradictions, the reviewer is notified 100% of the time; when it does not, no notice is shown.

## Assumptions

- **Terminology change is a pure rename**: "rejected" replaces "decayed" as the existing retirement state; no new fourth state is introduced. "Approved" and "active" are the same thing said two ways — approval is the action that lands a fact in the active state, not a separate state.
- **No production data to preserve**: the deployed facts store has no rows worth long-term compatibility shims (confirmed), so the rename is applied as a one-shot, idempotent data update rather than a maintained backward-compatibility layer. No read-time shim is built.
- **Single facts spine (post-refactor baseline)**: the codebase was collapsed onto one `facts` table as the single source of truth (commit `dbf60d9`). The separate `candidates` store (`CandidateStore`/`PostgresCandidateStore`) is gone; the dashboard candidate list, graph view, MCP context, and Contradictions tab all already read `facts` via the `FactsCandidates` projection (candidate id == fact id). The server is **Postgres-only** (no JSON offline store / 503 path). `fact_edges` is already actively used — contradiction edges are persisted on write and read back — so the link store is wired up, not merely present.
- **Extend existing routes, no parallel API**: because `GET/POST /candidates…` and `/contradictions…` are already facts-backed, this feature **extends those routes** (delete-state-gating, non-destructive resolve, pending/resolved edge kinds, the rename) rather than adding a parallel `/facts…` surface over the same table.
- **Backend first, dashboard second**: the lifecycle, links, and deletion gating land on the facts data layer first; the dashboard review experience follows. The candidate→facts repoint is already done by the refactor, so remaining frontend work is the `decayed`→`rejected` rename plus the pending/resolved contradiction UX — not a data-source switch.
- **Migration convention**: the repo now has a `migrations/` package (e.g. `m2026_06_23_unify_facts.py`) alongside the idempotent `schema.sql` bootstrap; the `decayed`→`rejected` data update follows whichever of those two conventions the plan selects.
- **Recommended review layout**: state-filter tabs over the fact table plus a per-fact contradiction panel (the proposal's Option 1) is the assumed dashboard layout, because it maps directly onto "review by state" and "facts contradicted by this fact." Alongside it, a dedicated global view (FR-013a) lists all pending contradictions for triage.
- **Contradiction detection is unchanged**: the quality/logic that *detects* contradictions is out of scope; only the *resolution* path (what happens to the loser) and review/deletion behavior change.

## Out of Scope

- Changing how contradictions are *detected* (the judging/flagging logic).
- Changing the semantics of the active/proposed states or the default passive contradiction-flagging behavior — only the resolution path changes.
- Renaming the active state to "approved".
- Converting the dashboard's candidate snapshot into a live projection of facts — **already completed** by the single-facts-spine refactor (commit `dbf60d9`); no longer part of this feature.
