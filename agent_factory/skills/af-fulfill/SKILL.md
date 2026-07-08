---
name: af-fulfill
description: >
  The interactive runtime that drives an END USER to complete a structured deliverable — the
  fact-gathering sibling of af-build. Where af-build drives an agent BUILDING SOFTWARE (verified by
  commands), af-fulfill drives a person SUPPLYING FACTS against a Praxis requirement graph until the
  derived completeness gate opens, then produces the deliverable. It runs a per-user-turn loop
  (GUARDRAIL the message → interpret it as an answer or a document → SETTLE every now-known
  requirement via record_outcome → when the MVP target is empty, run the deterministic evaluator and
  PRODUCE; else ASK the single highest-materiality open requirement through the budgeted ask channel),
  reading completeness LIVE from Praxis and failing closed. A domain is DATA (domains/<id>/*.yaml); a
  session is a per-taxpayer Praxis SPACE seeded from those files. Proving case #1 is tax-1040-2025 (a
  completed Form 1040 from a single W-2). Use to fulfill a structured deliverable for an end user —
  NOT to build software (that is af-build) and NOT to plan/admit requirements (that is af-intake-plan).
---

## The methodology — read first, this is the loop af-fulfill OWNS

State lives in ONE place: **Praxis**. There are no JSON status files, no on-disk flags. A requirement
is a Praxis fact (`category:"requirement"`, `source:"prd-<project>"`); completeness is **derived from
recorded outcomes** — the exact contract af-build uses (`incomplete_requirements` reads, `record_outcome`
writes). af-fulfill reuses that completeness spine and builds **no new gate engine** (KTD1).

**The ONE structural difference from af-build (KTD2):** the actor is an **end user in a chat**, not a
coding agent. So the loop is an **interactive request/response orchestrator**, NOT a Stop-hook-gated
autonomous loop. It advances **per user turn**, and the completeness gate is a **runtime check before
producing the deliverable** — never the `build_completeness` Stop hook (that is af-build's
autonomous-turn mechanism). You do not "keep working until done" inside one turn; you take exactly one
message, advance the graph as far as the new facts allow, and either ask one question or produce.

**A domain is DATA, not code (KTD4).** Everything domain-specific lives in `domains/<id>/`:
`requirements.yaml` (the requirement set + `cover`/`verify`/`renders`/`depends_on`/`guard`),
`fields.yaml` (the typed boundary), `rules.yaml` (rule tables as data), `compute.yaml` (the
calculation graph), `template.yaml` (how computed lines become the deliverable), `policy.yaml` (the
ask budget, defaults, guardrails). The runtime is generic; it takes a domain-dir path. The LLM never
does the math — a deterministic evaluator does (KTD3).

**A session is a per-taxpayer Praxis SPACE, seeded from files (KTD5).** Snapshots are space-scoped and
cannot be a cross-session template (proven by probe). So each session creates a fresh space and
**ingests the domain's requirement files** into it (`source:"prd-<project>"` + the pack meta), then
binds each rendering requirement's `renders` edge to the single deliverable surface (D9). Isolation is
the space; the gate (`completeness_summary`, `surface_coverage`) is computed over exactly that
session's facts.

**Praxis is a HARD dependency, fail-closed.** Unlike a degrade-to-fallback rule lookup, the af-fulfill
*control flow* reads completeness live, so Praxis-down BLOCKS a session. If the runtime client raises
`PraxisUnreachable`, STOP — never assume a requirement is covered, never produce past the gate, never
invent or cache state. (Local-dev seam: `PRAXIS_AUTH_DISABLED=1` + `PRAXIS_API_BASE_URL=http://localhost:8000`.)

---

# The runtime — the modules and what each owns

The generic runtime lives in `src/agent_factory/fulfill/` (tests mirror it in `tests/fulfill/`):

| Module | Owns |
|---|---|
| `domain.py` (U1) | `load_domain(path)` — parse + structurally validate the 7 pack files; fail loud naming the file+key. |
| `evaluator.py` (U2) | The closed-op calculation graph in `final`/`provisional`/`what_if` modes, per-line basis. The math. |
| `validate.py` (U3) | Field schemas + cross-field invariants (S6). Reject, never coerce. |
| `requirements.py` (U4) | fact→`FulfillRequirement` adapter, `resolve_cover`, materiality `rank_open` via what_if (S4). |
| `policy.py` (U9) | `Budget` (S1 — only `via=ask` decrements) + `Guardrails` scope refusals (S9, stable rule ids). |
| `praxis_client.py` (U5) | Runtime Praxis writes (create space / seed / bind) + reads, under the fail-closed contract (KTD7). |
| `session.py` (U6) | `start_session` — create the space, seed from files, bind `renders` edges; `Session` + `close()`. |
| `extract.py` (U7) | Document-extraction seam (W-2 → candidate facts), typed at intake. |
| `formfill.py` (U8) | Form-fill seam: provenance (S2), assumption receipt (S5), content hash (S10). |
| `loop.py` (U9) | `Conversation.handle_turn` — the gather orchestrator + the typed event trace (S3). |

The proving domain is `domains/tax-1040-2025/` — a basic federal 1040 from a single W-2. The evaluator
is validated against the reference oracle (`../agent_tax_harness/app/tax_engine.py`): $40k single →
taxable $24,250 → tax $2,672 → $528 refund (KTD8). The harness is an oracle, not a dependency.

---

# How to run a session

1. **Load the domain.** `domain = load_domain("domains/tax-1040-2025")`.
2. **Start the session** (creates + seeds the space). `session = start_session(domain, session_id)`.
   After seeding, both gates read green for a fresh session: `completeness_summary` = `0/N`,
   `surface_coverage(mvp)` = `0 uncovered`.
3. **Drive the gather loop.** `conv = Conversation(session)`; for each user message
   `result = conv.handle_turn(message)`. Each turn:
   - **GUARDRAIL** the message (S9). A 1099 / tax-advice request → a typed refusal naming the rule id,
     and **no fabricated line**. An SSN → redacted, the turn continues.
   - **Interpret** it — the answer to a pending question, else a **document** to extract (U7). Every
     extracted/answered value passes the typed boundary (U3) before it can become a fact.
   - **SETTLE + DEFAULT** — record every MVP requirement whose value is now known: cover-from-fact,
     the W-2 readback (`verify:user_confirmed`), or a policy default (a free S5 receipt line). Each
     `record_outcome` drops the requirement from the live incomplete set.
   - **ASK or PRODUCE.** When the MVP target is empty, the **completeness gate** opens → run the
     evaluator's `final` pass and produce the deliverable (PDF + provenance + receipt + hash). Else ask
     the single **highest-materiality** open requirement (U4) through the **budgeted ask channel** (S1)
     — inferring / defaulting / covering-from-a-fact is free; only an actual question decrements.

**The budget is enforced as code, not prompt (S1).** The runtime cannot ask past `policy.budget.max_asks`
— the act of asking is gated in `Budget.spend`, not in any instruction. Past the limit, remaining
requirements are **defaulted with documented assumptions**, not asked again.

**Producing before completeness is refused.** `Conversation.produce()` raises while any MVP requirement
is incomplete — the gate is a runtime check, the interactive analogue of af-build's Stop hook (KTD2).

**Every step emits a typed trace event (S3)** — `document_extracted`, `requirement_covered` (with its
cover source), `question_asked`, `readback`, `guardrail_refusal`, `budget_exhausted`,
`deliverable_produced`. The trace is the auditable record of how the bottom line was reached.

---

# The data-first domain contract (how to add or change a domain)

A domain is its `domains/<id>/` directory — change behavior by editing data, not the runtime:

- **requirements.yaml** — the requirement set. Each requirement: `cover` (ordered resolution: a
  `document:*` source, `user`, or `default:<k>`), `verify` (`schema_valid` | `fact_present` |
  `user_confirmed`), `renders` (the deliverable line(s) it grounds), optional `depends_on` and `guard`
  (a closed predicate — the requirement is only ASKED when it holds; it can still be defaulted), and
  `scope` (`mvp` drives the gate; `post-mvp` is parked).
- **fields.yaml** — the typed boundary every value passes before it is recorded.
- **rules.yaml / compute.yaml** — rule tables + the closed-op acyclic calculation graph. The evaluator
  reads these; it never hardcodes a number.
- **template.yaml** — `line_map` from compute step ids to the deliverable's lines; `identity_fields`;
  the provenance / receipt / hash policy.
- **policy.yaml** — `budget.max_asks`, the `defaults` (each with a justification for the receipt), the
  `ask_strategy`, and the `guardrails` (stable rule ids the generic runtime enforces).

`load_domain` validates cross-file integrity at load (every requirement field exists, every compute op
is in the closed vocabulary, every table op names a real table, every `line_map` key is a real step)
and fails loud naming the offending file + key. A malformed pack never reaches a session.

---

## Scope — what af-fulfill is and is NOT

- **IS:** the generic, domain-agnostic runtime that drives an end user to a completed deliverable,
  wired to live Praxis session-spaces, proven on `tax-1040-2025`.
- **IS NOT af-build:** it never touches the coding loop, the build gate, or `hooks/_ticket_state.py`.
- **IS NOT af-intake-plan:** it does not author or admit requirements, run planning validation, or save a
  `prd-<project>` snapshot. It CONSUMES the requirement files a domain ships.
- **Owned by Praxis (Q7), not this skill:** per-session space lifecycle, TTL/cleanup, auth hardening,
  multi-tenancy. `Session.close()` is the explicit teardown hook, not an automatic cleanup policy.

## Never

- **Never** let the LLM compute a number — the deterministic evaluator (`evaluator.py`) owns every
  line; a value with no non-LLM provenance is a hard assertion failure (S2).
- **Never** proceed when the Praxis client raises `PraxisUnreachable` — fail closed: stop, surface it.
- **Never** ask past the budget — `Budget` refuses the N+1-th question; default the rest with a
  documented assumption (S5 receipt).
- **Never** produce the deliverable before the completeness gate is open (the MVP target is empty).
- **Never** fabricate a line in response to out-of-scope input — emit the typed scope refusal naming
  the rule id (S9).
- **Never** query the incomplete/completeness endpoints with the `prd-` prefix — pass the BARE project
  name (the server prepends `prd-`).
- **Never** treat a Praxis snapshot as the cross-session template — seed each session-space from the
  domain files (KTD5).
- **Never** add domain logic to the runtime — a new behavior is a data edit to `domains/<id>/`.
