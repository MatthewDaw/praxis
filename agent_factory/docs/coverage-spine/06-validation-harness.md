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

## How you add one (one line, no file)
Both forms are the **add-check amend mode of `af-intake`** (the single write-path that owns
validation):
- **`af-intake` amend (insert only)** — inserts the check fact into Praxis, nothing else.
  The regress happens on the next `af-build`.
- **`af-intake` amend (insert + regress)** — inserts the check **and** regresses the
  matching tickets now (so they show incomplete immediately).

Example (illustrative — added only when *you* run the amend, never by the planning side):
> `af-intake` amend: "auth tickets need a live Playwright login test against the deployed service"

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
- The **`af-intake` add-check amend mode** is the *write* path into Praxis.

## Binding by class tag (caveat)
Binding by **requirement id** always works. Binding by **class tag** (`applies_to: auth`) only
matches requirements that carry that tag — `resolve_bindings` reads each requirement's `meta.tags`.
The `af-intake` insert-and-regress amend tags the matching requirements when it runs; otherwise bind by requirement id.
