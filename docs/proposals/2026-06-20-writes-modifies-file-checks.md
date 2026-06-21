# Proposal: faithful per-artifact grading (`output_file` + `writes_file` / `modifies_file`)

**Owner:** Dominic Antonelli — eval harness
**Status:** Accepted — core (§4–§8) and `StructuredOpenRouterRunner` §11 Phase 1 (output-side) implemented; §11 Phase 2 (fixture injection) pending
**Date:** 2026-06-20
**Scope:** `knowledge/evals` (EvalCase + EvalContext contracts, ClaudeCodeRunner, deterministic checks)

> Two related changes that make sandbox grading read the *agent's artifact*, not
> whatever blob the runner happened to assemble: **`output_file`** scopes the
> graded *content* to one file (§4.5); **`writes_file` / `modifies_file`** assert a
> file's *existence / change* (§4.1–4.4). They compose: one says *which file's
> content to grade*, the other says *that the agent produced it*.
>
> Both land on a backend-agnostic `artifacts` contract, which a follow-on
> **`StructuredOpenRouterRunner`** (§11) can satisfy via structured file output —
> recovering cheap-path coverage for ~6 file-writing cases we currently gate to the
> sandbox.

---

## 1. Problem

Every full-pipeline case grades a blob of text. In sandbox mode that blob is the
file the agent wrote; in single-shot mode it's the chat reply. Our deterministic
checks (`contains_text`, the indentation regexes, the `os.path`/`pathlib`
substring checks, the AST `function_calls`) all read that blob **without caring
where it came from**. Consequences:

- A chatty reply that *talks about* the answer passes the same substring checks as
  a real artifact. `defines_add: contains_text "def add"` is green whether the
  agent wrote `def add` into `calculator.py` or merely typed it in a chat message.
- When the agent never writes the expected file, `ClaudeCodeRunner` silently falls
  back to a box-sweep or the assistant's final text
  (`_collect_output`, [claude_code.py:226-253](../../knowledge/evals/claude_code.py)),
  so "no artifact produced" is **not** distinguishable from "correct artifact
  produced" at grading time.
- **The box-sweep grades the fixture too.** For a code case the runner can't find
  its single configured `output_file` (`poem.txt`), so it falls back to a sweep
  that concatenates *every* file in the box — including the mounted fixture. A
  check meant to grade `answer.py` actually reads `answer.py` + `utils.py` + …, so
  the fixture's own content can satisfy (or break) the check.

Nothing in the suite asserts *the agent actually wrote (or modified) the file the
case is about*, and content checks can't scope to *just that file*. This proposal
adds:

- `output_file` (per-case) — grade the *content* of one named artifact, not the
  whole box (§4.5).
- `writes_file(path)` — the agent **created** a new file at `path` (§4.3).
- `modifies_file(path)` — the agent **changed** a file that was mounted at `path` (§4.3).

## 2. The constraint that shapes the design

The box is a `tempfile.TemporaryDirectory` whose `return EvalContext(...)` happens
**inside** the `with` block
([claude_code.py:174-224](../../knowledge/evals/claude_code.py)). The directory is
deleted as `run()` returns — *before* `run_case_full` calls `run_checks`. So a
check **cannot** stat the filesystem post-hoc; the box no longer exists.

Therefore the artifact signal must be **captured during the run and carried on
`EvalContext`**. Checks then read that captured data, not the disk.

`EvalContext` already carries `output_source` (`named_file` | `box_sweep` |
`final_text` | `completion`) and `checkout_path`, but `output_source="named_file"`
only tracks the runner's *single* configured `output_file` (default `poem.txt`) —
it can't speak to an arbitrary per-case filename, and it can't distinguish
*created* from *modified*. So we need a new, richer field.

## 3. Goals / non-goals

**Goals**
- Assert that a specific file was created / modified by the agent, faithfully.
- Scope *content* checks to the artifact under test, not the whole box (fixtures
  included).
- Distinguish "produced the artifact" from "talked about it in chat."
- Keep default behavior unchanged — both pieces are opt-in per case; cases that
  set neither grade exactly as today.
- Stay offline-testable (CLI is already injected).

**Non-goals**
- No content assertions here — `writes_file` says *the file exists/changed*, not
  *what's in it*. Content is still the job of `contains_text`, the regexes, etc.
- No per-check capability gating. "Sandbox only" is expressed at the **case**
  level via `needs: [sandbox]` (see §6).
- No change to the single-shot or fake backends' behavior.

## 4. Design

### 4.1 New contract field

```python
# knowledge/evals/eval_def.py
class Artifact(BaseModel):
    path: str                                  # box-relative, posix ("calculator.py")
    status: Literal["created", "modified"]     # vs the mounted start state

class EvalContext(BaseModel):
    ...
    artifacts: list[Artifact] = Field(default_factory=list)  # files the agent produced/changed
```

Backward compatible: optional, defaults empty. A runner with nothing to report
(`FakeRunner`, `OpenRouterRunner`) leaves it `[]`.

### 4.2 Runner populates it (ClaudeCodeRunner only)

`ClaudeCodeRunner.run` already mounts fixtures, runs the agent, then collects
output. We insert a **snapshot before** the agent runs and a **diff after**:

1. After mounting (`mount_fixtures` + `fixture_path` copytree), walk the box and
   record `start = {relpath: sha256(bytes)}` for every file. This is the start
   state the agent will edit.
2. After `run_cli`, walk the box again → `end = {relpath: sha256(bytes)}`.
3. For each path in `end`:
   - not in `start` → `Artifact(path, "created")`
   - in `start`, hash differs → `Artifact(path, "modified")`
   - hash equal → omit (unchanged)
4. Attach the list to the returned `EvalContext`.

Notes:
- Hash **raw bytes**, so binary files and unreadable files still diff correctly
  (detection doesn't need to decode them, unlike the text sweep that builds `output`).
- Skip dotfile paths (`.git`, etc.), matching the existing sweep's convention.
- This is independent of which blob becomes `output`; `_collect_output` is
  unchanged. Artifacts is pure provenance alongside it.

### 4.3 The checks

```python
# knowledge/evals/deterministic_checks/builds.py
def writes_file(ctx, *, path: str) -> CheckResult:
    """Pass iff the agent CREATED a new file at `path` (box-relative)."""
    created = {a.path for a in ctx.artifacts if a.status == "created"}
    ok = path in created
    return CheckResult(name="writes_file", passed=ok, evidence=...)

def modifies_file(ctx, *, path: str) -> CheckResult:
    """Pass iff the agent MODIFIED an existing (mounted) file at `path`."""
    modified = {a.path for a in ctx.artifacts if a.status == "modified"}
    ok = path in modified
    return CheckResult(name="modifies_file", passed=ok, evidence=...)
```

Deliberately **non-overlapping**: `writes_file` = new, `modifies_file` =
changed-existing. (A future `touches_file` could mean "created OR modified" if a
case ever needs the union; not adding it speculatively.)

### 4.4 Semantics table

| File at `path` after the run | `writes_file` | `modifies_file` |
|------------------------------|:-------------:|:---------------:|
| created (wasn't in fixture)  | **pass**      | fail            |
| modified (was in fixture)    | fail          | **pass**        |
| unchanged (was in fixture)   | fail          | fail            |
| absent / never written       | fail          | fail            |
| runner records no artifacts (single-shot, fake) | fail | fail |

The last row is why these checks force a case onto the sandbox (§6).

### 4.5 Per-case `output_file` (content grading)

`writes_file`/`modifies_file` assert a file *exists/changed*; they say nothing
about its content. Content is still graded against `EvalContext.output` — and for
a code case `output` is the **box-sweep**, fixture and all (see §1). So a per-case
way to say "*grade this file's content*" is the other half of faithful grading.

Add an optional field to the **case**:

```python
# knowledge/evals/eval_def.py
class EvalCase(BaseModel):
    ...
    output_file: str | None = None  # box-relative artifact to grade; None => runner default
```

`ClaudeCodeRunner._collect_output` prefers `case.output_file` (when set) over the
runner's built-in default before falling back to the box-sweep. When set and the
file exists, `output` is *that file alone* and `output_source="named_file"`, so
every existing content check (`contains_text`, the regexes, `function_calls`)
scopes to the agent's artifact. If the file is absent, the existing fallback still
applies — and `writes_file` is what flags the absence.

**Measured motivation** (`fixture_reuse_existing_helper`, Haiku, neutral prompt):
with the reuse lesson the agent writes `from utils import slugify`; cold it
reimplements the slug inline. *Both arms currently PASS* `calls_slugify` and
`does_not_reimplement_slugify`, because the box-sweep includes the fixture's
`utils.py`, which itself contains `slugify(` and one `def slugify`. With
`output_file: answer.py` the checks read only the agent's file, and
`calls_slugify` becomes a clean reuse-vs-reimplement discriminator (reuse → the
call is present; inline reimplement → absent).

This composes with §4.1–4.4: `artifacts` says *which* files changed (existence);
`output_file` says *which file's content* to grade. A reuse-steering case wants
both — `writes_file(answer.py)` (the agent produced it) and `output_file: answer.py`
(grade its content).

## 5. Worked examples

- **`fixture_reuse_existing_helper`** (new file + content): the agent writes
  `answer.py`; the fixture ships `utils.py`. Set `output_file: answer.py` (so
  `calls_slugify` reads only the agent's file) **and** `writes_file(path: "answer.py")`
  (so an empty chat reply can't pass). This is the case that motivated the proposal
  — its current checks are fixture-contaminated (§4.5).
- **`iambic_poem`** (new file): add `writes_file(path: "poem.txt")` alongside the
  meter check. Now "wrote the poem to the file" is asserted, not assumed.
- **Edit-in-place case** (a fixture source file the agent must change): use
  `modifies_file(path: "<that file>")` to assert the edit landed — pair it with a
  content check (scoped via `output_file`) that asserts *how* it changed.

## 6. Capability gating

A case carrying `writes_file`/`modifies_file` only makes sense on a runner that
provides a sandbox; everywhere else `artifacts` is empty and the check fails.
Two ways to keep that honest:

- **Explicit (minimum):** author adds `needs: [sandbox]` to any case using these
  checks. Simple, no magic. Risk: forget it → the case FAILs on OpenRouter
  instead of SKIPping.
- **Auto-derive (recommended safeguard):** extend `case_needs`
  ([run.py:124-140](../../knowledge/evals/run.py)) to add `"sandbox"` when any
  deterministic check ref ends in `:writes_file` or `:modifies_file`. Removes the
  footgun at the cost of a small name-coupling in `case_needs`.

Recommendation: do the auto-derive — it's a few lines and prevents a whole class
of "phantom FAIL on the wrong backend" misconfig, consistent with the existing
fixtures→sandbox auto-derivation.

## 7. Implementation plan

1. `eval_def.py`: add `Artifact` model + `EvalContext.artifacts` (default empty),
   and `EvalCase.output_file` (default `None`).
2. `claude_code.py`: snapshot-after-mount, diff-after-run, attach artifacts; and
   have `_collect_output` honor `case.output_file` before the box-sweep fallback.
   Factor the box walk into a small `_hash_tree(workdir) -> dict[str, str]` helper.
3. `builds.py`: add `writes_file` + `modifies_file`.
4. `run.py`: extend `case_needs` to auto-derive `sandbox` for these checks (§6).
5. Adopt in cases: rework `fixture_reuse_existing_helper` (neutral prompt +
   `output_file: answer.py` + `writes_file` + a before-control), and add
   `writes_file` to `iambic_poem`. Others opt in case-by-case.
6. Tests (all offline).

## 8. Testing strategy

All offline via the injected `run_cli`. The fake CLI writes (or doesn't write)
files into the box, exactly as the existing
`test_runner_copies_fixture_into_box` does
([test_claude_code.py:72-91](../../knowledge/evals/tests/test_claude_code.py)).

- **Runner records created file**: fake CLI writes a new `answer.txt` →
  `ctx.artifacts == [Artifact("answer.txt", "created")]`.
- **Runner records modified file**: fixture has `calculator.py`; fake CLI appends
  to it → status `"modified"`.
- **Unchanged file is omitted**: fixture file the CLI doesn't touch → not in
  `artifacts`.
- **Check unit tests** (`test_builds.py`): `writes_file` pass on created / fail on
  modified, absent, and empty-artifacts; `modifies_file` mirror.
- **Gating**: a case with `writes_file` is partitioned as skipped under
  `OpenRouterRunner`/`FakeRunner` with reason `needs 'sandbox'`.
- **`output_file` scoping**: fixture ships `utils.py`; fake CLI writes `answer.py`.
  With `output_file: answer.py`, `ctx.output` is `answer.py`'s content only
  (`output_source="named_file"`) and a substring present only in `utils.py` is not
  matched; unset, the box-sweep includes both.

## 9. Risks & alternatives

- **Box-walk cost.** Negligible — per-case eval, a handful of files, hashed once
  before and once after.
- **Hashing vs mtime.** Hash is robust to "rewrote identical content" (correctly
  → unchanged) and to clock quirks; mtime would be cheaper but lie. Hash wins.
- **Alternative A — `graded_from_artifact` (free, coarse).** A check that passes
  iff `output_source in {named_file, box_sweep}`. Zero harness change, catches the
  pure-chat-reply case, but can't name the file or tell created from modified.
  Strictly weaker; rejected because the ask is "the *correct* file."
- **Alternative — keep the box alive until after checks.** Move check execution
  inside the box lifecycle so a check can stat disk. Larger blast radius (changes
  the run/grade ordering for *all* cases) for no extra capability over capturing
  artifacts. Rejected.
- **Name-coupling in `case_needs`** (auto-derive) is mild magic. Acceptable and
  mirrors the existing fixtures→sandbox rule; documented in the function.

## 10. Decisions (resolved)

1. **`modifies_file` is strict** — a freshly *created* file does not count; use
   `writes_file` for that. Keeps the created/modified split crisp. (A `touches_file`
   union can be added later if a case needs it.)
2. **`deletes_file` is deferred** — no case needs it yet; trivial to add later
   (path in `start` but not in `end`).
3. **`artifacts` flows into the transcript** — carried on `AgentRun` so a failed run
   shows exactly what the agent wrote.
4. **`output_file`-absent falls back to the box-sweep** — `writes_file` is the
   existence assertion that flags the missing file; content checks stay orthogonal
   (so you get a granular `FAIL (n−1/n)` pinpointing the absence, not a blank).
5. **Auto-derive the need (§6)** — a `writes_file`/`modifies_file` check adds
   `sandbox` to `case_needs` automatically, so the case SKIPs (not FAILs) on a
   backend that can't populate `artifacts`.

Implementation note: the probe harness (`test_probes.py`) treats a probe string for
an `output_file` case as that file's content and synthesizes a matching `created`
artifact, so `writes_file`/`output_file` cases stay probe-able with plain good/bad
strings.

## 11. Follow-on: `StructuredOpenRouterRunner` (output-side `file_io`)

Once both runners emit an artifact-bearing `EvalContext` (§4.1), OpenRouter can
*also* produce one — without a real box — by asking the model for **structured
file output** and parsing it into a virtual file tree. This recovers cheap-path
coverage for the file-writing cases we currently gate to the sandbox, with
*cleaner* grading than the box-sweep (we read JSON `contents`, never chat prose).

### 11.1 What it un-gates

Of the 68 cases today, 52 already run on OpenRouter. Of the 16 gated ones:

| Class | Un-gated by | Count | Examples |
|-------|-------------|------:|----------|
| **A — output side only** (no fixture, writes files) | structured `file_changes` | **+6** | `iambic_poem`, `scoped_conflict`, `safety_user_overrides_graph`, `pathlib_preference`(+`_before`), `decayed_lesson_ignored`† |
| **B — output + input side** (starts from a fixture) | also serialize fixtures into the prompt | +9 | the `fixture_*` cases |
| **C — needs real execution** | nothing single-shot | 1 | `exactly_n_negative` (`code_task`) |

† red spec — un-gating lets it XFAIL on OpenRouter instead of skipping.

**Phase 1 is output-side only (+6 → 58/68).** Those 6 are exactly the cases this
session gated; the structured runner buys them back on the cheap path. Phase 2
(input-side fixture serialization, +9) carries a fidelity caveat (§11.5) and is
optional.

### 11.2 Capability refinement: split `sandbox`

`sandbox` currently conflates "can mount + grade files" with "can execute code."
The structured runner does the former, not the latter, so split it:

| Capability | Meaning | ClaudeCode | StructuredOpenRouter | plain OpenRouter |
|------------|---------|:----------:|:--------------------:|:----------------:|
| `file_io`  | mount fixtures + produce gradeable file artifacts (real box **or** virtual) | ✓ | ✓ | — |
| `code_exec`| run code / a test oracle | ✓ | — | — |

`case_needs`: fixtures / `output_file` / `writes_file` / `modifies_file` → `file_io`;
`code_task` → `code_exec`. So file-grading cases run on **both** Claude and
structured-OpenRouter; execution cases stay Claude-only. (The auto-derive in §6
changes from deriving `sandbox` to deriving `file_io`.)

### 11.3 Design

The runner makes the single-shot call request a structured result and parses it
into the same `EvalContext` shape every check already understands:

```python
# response schema (json_schema, strict)
{ "file_changes": [ { "path": "answer.py", "contents": "..." } ],
  "notes": "free-text commentary, kept OUT of contents" }
```

1. Build the user turn; for Phase 2, serialize mounted fixtures as
   ``path:\n```\n<body>\n``` `` blocks ahead of the task.
2. Call with `response_format: {type: "json_schema", strict: true}`.
3. Parse `file_changes` into a virtual tree; diff against the input fixtures
   (empty for Phase-1 cases) → the same `artifacts` (`created`/`modified`) as §4.2.
4. Select `output` via `case.output_file` (§4.5); `output_source="named_file"`.

Result: `writes_file`, `modifies_file`, `output_file`, and every content check
work identically to a Claude run — the checks never learn which backend ran.

### 11.4 Model reliability — no registry, classify at runtime

`provides = {file_io}` is **runner-level and unconditional** — it's a property of
the runner, not the model. Whether a *given model* reliably emits valid
`file_changes` is a **runtime** fact, so we do **not** maintain a model allowlist
(it rots; "supports the param" ≠ "enforces it" — the 20–40% flake). Instead:

- `serves_model` stays static (just the `provider/model` id shape).
- Request `strict` json_schema so compliant providers **enforce server-side**.
- Validate at parse; non-compliance → a **loud, specific runner ERROR classified as
  a capability miss** (`model X did not return valid file_changes for case Y`),
  recorded as ERROR/SKIP-with-reason — **never a content FAIL** that pollutes the
  scoreboard.
- *Optional later:* learn malformed-rate empirically and auto-skip chronically
  flaky models — still no hand-maintained list.

### 11.5 Fidelity discipline

The structured runner is a **third tier** (structured single-shot) between plain
single-shot and a real agent — not a merge that erases the difference:

- **Output side (Phase 1)** is faithful for closed, single-file *transforms*: the
  model emits the whole artifact; we grade its bytes. Low risk.
- **Input side (Phase 2)** changes the *stimulus* — spoon-feeding every fixture
  in-context is not the same as an agent navigating a workspace (e.g.
  `fixture_reuse_existing_helper`: injecting `utils.py`'s body is a much louder
  hint than the agent choosing to read it). Plus full-file re-emit is lossy on
  large/multi-file fixtures ("// unchanged below").
- **Discipline:** before trusting the structured runner for a case, run it on both
  backends and confirm the verdicts agree (the cross-runner matrix). Agreement →
  faithful enough; divergence → the boundary, keep it Claude-only.

### 11.6 Sequencing

Phase 2 of this proposal — it builds on the `artifacts`/`output_file` contract
(§4), so land that first. Phase 1 (output-side, the +6) is the high-value,
low-risk slice; Phase 2 (input-side, the +9) is optional and caveated.

### 11.7 Status — Phase 1 implemented & verified

`StructuredOpenRouterRunner` (output-side) is built: `provides = {file_io}`,
`response_format` json_schema (strict), `file_changes` parsed into `artifacts` +
`output` (via `output_file`), malformed output → loud capability error. The
`sandbox` capability was split: `ClaudeCodeRunner` now provides `{sandbox, file_io}`,
the six fixture-less file-writing cases moved to `needs: [file_io]`, and the
`writes_file`/`modifies_file` auto-derive yields `file_io`. CLI: `--structured`.

Live run (gpt-4o-mini) of all six `file_io` cases: `pathlib_preference` (PASS,
pathlib) and `pathlib_preference_before` (PASS, os.path) — the discriminator
reproduces on the cheap backend; `scoped_conflict` PASS (tabs survive in JSON
`contents`); `safety_user_overrides_graph` PASS (the case single-shot used to
mis-run); `iambic_poem` PASS (`writes_file` + meter); `decayed_lesson_ignored`
XFAIL (red spec). Verdicts agree with the ClaudeCode runs, so fidelity holds for
this slice. Fixture cases (`needs: sandbox`) correctly skip the structured runner.
