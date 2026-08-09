# The Validation Harness (coding-agent side)

> Companion to [`00-overview.md`](00-overview.md). The validation instantiation of the coverage
> spine: a live check bound to a ticket, with a fail→regress→re-pick loop. Deterministic core:
> `src/agent_factory/validation_target.py` (tested in `tests/test_validation_target.py`).

## The model (read this first)
**What gets tested lives ENTIRELY in Praxis** — the validation graph holds the checks. The skill
and harness files are *generic*: they only say **how to run** a check and **how to pull the
applicable checks from Praxis** for any situation. No check content ever lives in a file.

```
insert a check in Praxis bound to a ticket
  -> the ticket is now validation-INCOMPLETE (a bound check isn't passing)
  -> the factory regresses it (record_outcome "failed") -> it re-enters incomplete_requirements
  -> build_completeness_gate forces the coding agent to re-pick it
  -> af-build PULLS the ticket's checks from Praxis and RUNS each (meta.run, exit code = verdict)
  -> only when every bound check passes does the ticket count complete again
```

## How a check is stored (Praxis, never a file)
A validation check is a Praxis fact:

```
category = "check"
scope    = "validation"
source   = "prd-<project>"
text     = "<criterion — what must be true>"
meta     = { check_id:   "<stable-slug>",
             applies_to: "<requirement-id | class-tag e.g. auth>",
             run:        "<command; non-zero exit = fail>" }
```

`incomplete_requirements` filters `category="requirement"`, so checks never pollute it.

## How you add one (one command, no file)
The write path is the **`af-ingest author-check`** command — the R1a plan-time entry point that owns
the `building-validation` snapshot (its planning sibling is `af-ingest author-lens`, which writes
`planning-validation` and re-arms the blessing audit). The `af-intake-build-validation` /
`af-intake-plan-validation` skills that used to hold these were deleted: a skill is prose a caller may
or may not follow, so it could not enforce the authenticated identity or the content hash-pin that
every check now carries. Deleting them left the replacement **named nowhere an operator could run** —
for a while the only surviving reference was a Python function name, which an agent at a shell cannot
invoke. The runnable forms, in order of preference:

```sh
af-ingest author-check "<criterion>" --project <project> --applies-to auth --run "<command>"
# no factory install on PATH? same code, no install:
python -m agent_factory.ingestion_api author-check "<criterion>" --project <project> --run "<command>"
```

`--applies-to` is a comma-separated tag list; omit it for the `*` wildcard. `--rubric` (JSON) makes it
a graded check instead of a binary one; `--surfaces` binds it to surface ids. The command prints the
written fact's `{"id", "action"}` as JSON and exits non-zero on refusal.

Two forms of the write itself:
- **insert only** — `af-ingest author-check …` writes the check fact into Praxis, nothing else.
  The regress happens on the next `af-build`.
- **insert + regress** — follow it with `ingestion_api.regress_for_check(project, ticket_ids, check_id, entry)`
  (Python API; no CLI shell) so the matching tickets show incomplete immediately.

Example (illustrative — added only when *you* run the amend, never by the planning side):
> `af-ingest author-check "auth tickets need a live Playwright login test against the deployed service" --project <project> --applies-to auth --run "npx playwright test …login…"`

→ a `check` fact (`applies_to: auth`, `run: "npx playwright test …login…"`) is written to Praxis,
the `auth` requirements are tagged + regressed, and they re-enter the build set.

## What's built (live end-to-end)
- **Deterministic core (tested):** `validation_target.py` — `checks_from_facts` (build checks from
  Praxis fact dicts), `resolve_bindings` (id or class-tag), `select_validation_incomplete` (the
  regress set), `ValidationState`, `unbound_checks`.
- **`af-build` (build start):** pulls the validation checks from Praxis at build start, binds them, and
  `record_outcome("failed")` on any bound-but-not-passing ticket that shows complete (the trigger).
- **`af-build` (verify):** for the ticket being verified, pulls its bound checks from Praxis and
  runs each `meta.run` as a **blocking external signal**; the ticket records `"succeeded"` only when
  generic gates **and** every bound check are green.
- **`build_completeness_gate`** (unchanged) forces the re-pick.
- **`af-ingest author-check`** is the *write* path into Praxis.

## Binding by class tag (caveat)
Binding by **requirement id** always works. Binding by **class tag** (`applies_to: auth`) only
matches requirements that carry that tag — `resolve_bindings` reads each requirement's `meta.tags`.
Nothing on the current write path adds a tag — `af-ingest author-check` writes only the check, and
`regress_for_check` only attaches the regression evidence — so a class tag binds only requirements that
already carry it. Bind by requirement id (or surface) unless the tag is already on the plan.
