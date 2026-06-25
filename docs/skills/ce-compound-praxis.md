# ce-compound-praxis (skill)

**Canonical, version-controlled source for the `ce-compound-praxis` skill.** Claude Code
loads skills from `.claude/skills/`, which is gitignored in this repo, so this tracked
copy is the shareable source. To install locally:

```bash
mkdir -p .claude/skills/ce-compound-praxis
cp docs/skills/ce-compound-praxis.md .claude/skills/ce-compound-praxis/SKILL.md
```

(Keep the two in sync when editing — this `docs/` copy is the source of truth; the
`.claude/` copy is the local install.)

The skill is the session-end capture trigger for the ce-compound→Praxis bridge: it
renders the current session's solve narrative and hands it to the `praxis_ingest_session`
MCP tool, which distills it server-side into human-gated `proposed` candidates. It is the
`/ce-compound` loop writing to the Praxis knowledge graph instead of `docs/solutions/`.

---

```markdown
---
name: "ce-compound-praxis"
description: "Capture a just-solved problem from the current session into Praxis as proposed knowledge candidates (the /ce-compound loop, but writing to the Praxis knowledge graph instead of docs/solutions/). Renders the session's solve narrative and hands it to the praxis_ingest_session MCP tool, which distills it server-side into human-gated proposed candidates. Use after fixing a non-trivial, verified problem when you want the lesson retrievable by future sessions. Triggers: 'compound this to praxis', 'capture this into praxis', 'ce-compound-praxis'."
argument-hint: "[optional: brief context hint]"
metadata:
  author: "Dom Antonelli"
user-invocable: true
disable-model-invocation: false
---

## User Input

\```text
$ARGUMENTS
\```

`$ARGUMENTS` is an optional one-line context hint pointing at which solved problem to
capture (e.g. "the yoyo import fix"). If empty, use the most recent solved-and-verified
problem in this session's history.

## What this does

Praxis is this repo's self-improving knowledge loop. `/ce-compound` writes a markdown
doc to `docs/solutions/`; **this skill writes to the Praxis knowledge graph instead**,
so the lesson is retrievable by future sessions through `praxis_get_context`. It is the
session-end sibling of the PR-distilling `CommitIngestor` — the same extractor, reached
from a live solved-problem session rather than a merged PR.

The distillation runs **server-side**: this skill renders the session's solve narrative
and hands the whole thing to the `praxis_ingest_session` MCP tool. The backend runs the
`SessionIngestor` distiller and writes each durable insight as a **`proposed`**
candidate — staged for human review in the dashboard, **not** added active. (The MCP
`praxis_add_insight` tool is the other path — a single, already-distilled fact you want
stored at full confidence; use this skill for distilling a whole session.)

## Preconditions

- The problem is **solved and verified** in this session (not in progress).
- It is **non-trivial** — a durable lesson worth retrieving later (a gotcha, a
  convention, a decision + rationale, a rejected approach), not a typo or obvious fix.
- The Praxis MCP server is registered and you are logged in. If a data tool reports
  "not logged in," ask the user for their Praxis email/password and call `praxis_login`
  first (see `docs/matt/MCP_SERVER.md`).

If the problem is not yet solved/verified, say so and stop — there is nothing durable to
capture yet.

## Workflow

### 1. Render the solve narrative

From this session's history, reconstruct the solved-problem narrative as a single text
block, using the same section shape `/ce-compound` extracts:

- **Bug-shaped:** Problem · Symptoms · What Didn't Work · Solution · Why This Works ·
  Prevention.
- **Knowledge-shaped:** Context · Guidance · Why This Matters · When to Apply · Examples.

Keep it the *durable* story — the root cause, the gotcha, the decision and why it holds,
the convention it established, approaches tried and rejected. Don't transcribe the
tool-by-tool play-by-play; the backend distiller ignores it, but a tighter narrative
distills better. Resolve pronouns and "this"/"it" to concrete subjects so each fact will
stand on its own.

Do **not** distill into one-line facts yourself — hand the backend the narrative and let
`SessionIngestor` do the structured extraction (one extractor, server-side).

### 2. Write to Praxis (proposed candidates)

Call the MCP tool with the rendered narrative:

\```text
praxis_ingest_session(narrative="<the rendered solve narrative>")
\```

- Omit `source` and the backend generates a `session/<id>`; pass `source="session/<id>"`
  only if you have a meaningful id.
- The tool returns a summary plus the created candidates (`id` / `scope` / `category`).
- Candidates land **`proposed`**. Promotion is a **two-step `proposed → active`** human
  gate in the dashboard — this skill never lands `active` knowledge directly.

If the tool fails soft with a login hint, run `praxis_login` and retry. If it returns a
size error (413), the narrative is too long — tighten it to the durable story and retry.

### 3. Keep a provenance doc (optional)

The graph fact is the record. If a human-readable artifact is wanted, you may also write
the rendered narrative to `docs/solutions/` as **provenance** — but it is a non-authoritative
log, not the source of truth, and is optional. Do not treat it as a second knowledge store.

### 4. Report

Summarize: how many `proposed` candidates were created, their `source`, and their
`scope`/`category`. Remind the user they are staged for review in the Praxis dashboard
(proposed → active) and retrievable via `praxis_get_context` once approved.

## Notes

- **Proposed, not active.** Self-captured session knowledge enters the human-gated
  candidate lifecycle, so it is reviewable, dedup'd, and reconciled like every other
  candidate — not written straight to the active store.
- **Backend distillation.** This skill renders and POSTs; the structured distillation and
  the graph write happen in the backend (the MCP process holds no DB creds).
- **Secrets.** The narrative is sent to the backend (and its LLM) and scrubbed by the
  write-policy `Redactor` before storage — but avoid pasting raw secrets/credentials into
  the narrative in the first place.
```
