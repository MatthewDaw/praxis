---
name: af-clean
description: >
  Portable aggressive AI-slop cleanup for ANY repository, most of which never went through
  af-build. Detects the repo's toolchain, derives an exemption manifest, allocates LLM judgment by
  deterministic census, produces LOCATED findings, admits them through a gate that drops
  unlocated or underspecified ones, verifies each proposed deletion blind (a verifier subprocess
  that sees only the diff, never the reasoning that produced it), applies only what clears the
  witness-tiered gate, and lands the result as a risk-stratified commit stack that can be unwound
  by revert alone. Use when a human asks to clean up AI slop, strip dead or defensive code,
  triage stale comments, or tighten a repo — "/af-clean", "clean this repo", "remove the slop".
  This is E1, the human entry point; E2 is the same engine invoked by af-build's minimalism-dry
  axis over a single ticket's diff, and the two never collapse into one code path.
---

# af-clean — E1, the human entry point

`/af-clean [path...]` — whole repo by default, or the subtree you name. It runs against arbitrary
repositories, so **assume nothing about the project**: no af-build history, no Praxis plan, no
conventions file, possibly no test suite.

Everything below is a real module in `agent_factory.af_clean`. Call the machinery; do not
reimplement its judgment in prose.

## The invariant that makes this safe to run on someone else's repo

**A finding is a claim about a LOCATION, and an unlocated claim is not a finding.** The admission
gate (`findings.admit_finding`) drops anything without a file+line or without enough specificity to
act on. This is what keeps an aggressive cleaner from turning into a vandal: it can only delete
what it can point at.

Deletion additionally requires **evidence you did not generate**: the blind verifier
(`verifier.run_verifier`) sees the diff and the repo, never your reasoning, so it cannot be talked
into agreeing with you. A deletion that only you believe in does not ship.

## The run

1. **Resolve scope.** `entry.resolve_scope(repo_root, path)` — the repo root when the caller named
   nothing, the named subtree otherwise.
2. **Detect the toolchain.** `toolchain.detect_toolchain(repo_root)` — per-invocation, zero-install
   probing. What you may run (tests, linters, type-checkers) is whatever this reports, not what you
   assume from file extensions.
3. **Derive exemptions.** `exemptions.derive_exemption_manifest(repo_root)` — vendored, generated,
   and fixture paths are exempt automatically. Do NOT hand-maintain this list; a repo you have
   never seen has its own conventions.
4. **Allocate judgment.** `census.run_census(...)` over `census.discover_source_files(repo_root)` —
   the census is deterministic and decides where LLM judgment is spent. Spending it everywhere is
   how a cleanup run becomes unaffordable and unfocused.
5. **Measure BEFORE touching anything.** `reachability.build_symbol_graph(repo_root)` and
   `reachability.collect_coverage(...)` — a read-only pass producing a tri-state deletion verdict.
   Tri-state matters: "unreachable", "reachable", and "cannot tell" are three different answers, and
   the third is not a licence to delete.
6. **Produce candidate findings**, then admit them: every candidate goes through
   `findings.admit_finding` and only the admitted ones survive. Useful producers:
   - `af_clean_comment_triage.classify_comment` — comments are triaged by INFORMATION GAIN, never
     by density. A comment that restates the identifier is slop; a comment carrying a reason,
     a caveat, or a link is not, however verbose.
   - `af_clean_scar_detection.detect_scar` — defensive code with a commit behind it is a SCAR, not
     slop. Blame the lines before proposing removal: code added by a bug-fix commit is load-bearing
     evidence that the defence is real.
   - `af_clean_string_corpus` — string-dispatch corpus with whole-token quarantine matching.
7. **Verify blind.** `verifier.build_verifier_payload(...)` → `verifier.run_verifier(...)` →
   `verifier.parse_verifier_output(...)`.
8. **Apply only through the witness gate.** `af_clean_witness` — the applier is witness-tiered and
   the aggression dial is pinned at its floor. Findings that do not clear their tier are REPORTED,
   not applied.
9. **Commit as a risk-stratified stack.** `commit_stack` — layered by risk so a regression is undone
   by reverting one layer, not by unpicking a mixed commit. It refuses forbidden git operations
   (`ForbiddenGitOperation`) and its unwind is safe in a shared checkout.
10. **Validate and remediate.** `af_clean_validate.run_validation_and_remediation(...)` — E1 invokes
    this ITSELF, because nothing else in an ad-hoc repo run will. (E2 does not: af-build already
    validates at end of run.)

`entry.run_e1(repo_root, path, produce_findings=..., apply_findings=..., dry_run=...)` wires
6→10 together and returns an `E1Result` carrying findings plus the validation report.

## Just run it

```bash
python -m agent_factory.af_clean [path] [--repo-root DIR] [--apply] [--no-validate]
```

`producers.default_producer()` supplies step 6 and the CLI supplies 1–5 and 10, so the command
works with no wiring. **Dry run is the default** — `--apply` is the explicit opt-in, because the two
mistakes are not symmetric: a dry run that should have applied costs one command, an apply that
should not have run edits someone's repository.

Step 10 makes E1 slow on a large repo: it discovers and runs the project's real test suite. That is
the point — findings that break the build are not findings — but use `--no-validate` when you only
want the report.

## Report advisorily — there is no verdict to give

`E1Result` deliberately has **no top-level pass/fail field**. This is someone's repository, not a
ticket, and there is nothing for a verdict to gate. Report what was found, what was applied, what
was skipped and why. Do not editorialise about code quality beyond the located findings.

## Dry run

`dry_run=True` skips the apply step and produces the **exact same findings** a live run would —
dry-run changes what is written, never what is found. Offer it first on a repo you have not cleaned
before, and on any repo with uncommitted work.

## Never

- Never delete on your own say-so — blind verification is not optional and cannot be self-served.
- Never apply an advise-tier finding; report it.
- Never hand-write the exemption list, or edit a path the manifest exempts.
- Never collapse E1 and E2 into one path: E2 is diff-scoped by construction and must not be able to
  reach outside its ticket's diff.
- Never treat "cannot tell" from the reachability pass as unreachable.
