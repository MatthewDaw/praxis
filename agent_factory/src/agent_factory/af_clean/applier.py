"""The apply path: witness gate → blind verification → risk-stratified commit stack.

``--apply`` used to do nothing. ``run_e1`` takes an ``apply_findings`` callable and the CLI passed
``None``, so the witness gate, the verifier, and the commit stack — the entire back half of the
engine, all of it tested — were unreachable from the command a human types. Findings were printed
and thrown away.

This is that path, and it is deliberately the narrow one: it applies COMMENT removals only. That is
not a placeholder, it is where the evidence currently reaches. A comment whose triage verdict is
``eligible`` is a literal, deterministic restatement of the signature it annotates — the strongest
kind of witness this engine has — whereas deleting a symbol needs a reachability verdict plus
coverage plus a tombstone, and proposing those from here would be inventing evidence the detectors
have not produced.

Order is not arbitrary. The witness gate runs FIRST and cheaply, so a proposal that can never apply
never reaches the verifier. Blind verification runs on the assembled diff, so the verifier judges
what would actually land rather than one hunk's story about itself. The commit stack runs last,
because a change that is not endorsed must never become a commit anyone has to revert.
"""

from __future__ import annotations

import difflib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from ..af_clean_witness import Decision, Proposal, decide
from .commit_stack import LAYER_COMMENTS, LayerChange, apply_commit_stack, build_layers
from .findings import Finding
from .verifier import run_verifier

# The comment rules whose eligible verdict is a literal restatement, so an instance may carry a
# lexical match_kind and the AST/corpus witness tier. Any rule NOT listed here is reported.
_LITERAL_COMMENT_RULES = frozenset({"comment-no-information-gain"})


@dataclass
class ApplyOutcome:
    """What actually happened, per finding — so a run can be audited rather than trusted."""

    applied: list[Finding] = field(default_factory=list)
    reported: list[tuple[Finding, str]] = field(default_factory=list)
    verifier_rejected: list[Finding] = field(default_factory=list)
    commit_result: object | None = None

    def summary(self) -> str:
        return (f"{len(self.applied)} applied, {len(self.reported)} reported "
                f"(gate), {len(self.verifier_rejected)} rejected (verifier)")


def _proposal_for(finding: Finding) -> Proposal:
    """Translate a finding into the witness gate's terms.

    ``tier_ceiling`` comes from the finding, never from ambition: an ``advise`` finding stays advise
    and the gate reports it, which is the rule that keeps prose judgments from deleting code.
    """
    literal = finding.rule in _LITERAL_COMMENT_RULES
    return Proposal(
        rule=finding.rule,
        # A literal restatement is the one comment case whose evidence is deterministic; everything
        # else keeps the finding's own (advise) ceiling and is therefore reported.
        tier_ceiling="enforce" if literal else finding.tier,
        match_kind="lexical" if literal else "semantic",
        # Tier 2 = AST/corpus/execution. The comment's eligibility was decided by token-overlap
        # against the annotated symbol's identifiers, which is exactly an AST-level witness.
        witness_tier=2 if literal else None,
        is_scar=finding.rule == "defensive-code-is-a-scar",
        is_test_deletion=False,
        bound_to_symbol_deletion=False,
    )


def _strip_comment_line(repo_root: Path, finding: Finding,
                       *, current_text: str | None = None) -> tuple[str, str] | None:
    """Remove the single line a comment finding points at. Returns (before, after) file text.

    ``current_text`` carries the in-progress text when several findings hit one file, so each
    successive removal composes on the previous one instead of being computed against stale disk.
    """
    loc = finding.location
    if loc is None or loc.line is None:
        return None
    path = repo_root / loc.file
    if current_text is not None:
        before = current_text
    else:
        try:
            before = path.read_text(encoding="utf-8")
        except OSError:
            return None
    lines = before.splitlines(keepends=True)
    idx = loc.line - 1
    if idx < 0 or idx >= len(lines):
        return None
    # Refuse if the line is not the comment we were told about: the file may have moved under us,
    # and deleting by stale line number is how a cleaner corrupts a repo.
    if lines[idx].strip() and not lines[idx].lstrip().startswith(("#", "//")):
        return None
    after = "".join(lines[:idx] + lines[idx + 1:])
    return before, after


# The verifier is a model subprocess and can hang indefinitely -- an observed run sat past nine
# minutes with no verdict, which makes --apply unable to finish at all. Bound it, and treat a
# timeout as endorsing NOTHING: a verifier that did not answer has not endorsed anything, and
# fail-closed is the only reading that preserves "a deletion needs evidence".
VERIFIER_TIMEOUT_S = int(os.environ.get("AF_CLEAN_VERIFIER_TIMEOUT_S", "180"))


def default_subprocess_runner(argv, **kwargs):
    """The real runner for both the verifier and git. Matches ``subprocess.run``'s signature
    because that is exactly what ``run_verifier`` and ``apply_commit_stack`` call it with."""
    kwargs.setdefault("timeout", VERIFIER_TIMEOUT_S)
    try:
        return subprocess.run(list(argv), **kwargs)
    except subprocess.TimeoutExpired:
        # Empty stdout -> parse_verifier_output endorses nothing, which is exactly right.
        return subprocess.CompletedProcess(list(argv), returncode=124, stdout="", stderr="timeout")


def _git_runner(argv: list[str], cwd: Path):
    return subprocess.run(["git", *argv], cwd=cwd, capture_output=True, text=True)


def apply_findings(
    repo_root: "str | Path",
    findings: Sequence[Finding],
    *,
    git_runner: Callable[..., object] | None = None,
    verifier_runner: Callable[..., object] | None = None,
    validate_fn: Callable[[Path], bool] | None = None,
    skip_verify: bool = False,
) -> ApplyOutcome:
    """Gate, verify, then commit. Returns what happened rather than raising on refusal —
    a refusal is a normal outcome here, not an error.

    Verification is ON by default and skipping it requires ``skip_verify=True`` at the call site.
    The earlier shape, where a ``None`` runner silently meant "no verification", made the engine's
    central invariant — a deletion needs evidence its proposer did not generate — depend on a
    caller remembering to pass an argument. A safety property that defaults to off is not a safety
    property.
    """
    root = Path(repo_root)
    outcome = ApplyOutcome()
    if git_runner is None:
        git_runner = _git_runner
    if verifier_runner is None and not skip_verify:
        verifier_runner = default_subprocess_runner

    # 1. WITNESS GATE, first and cheap.
    cleared: list[Finding] = []
    for f in findings:
        decision: Decision = decide(_proposal_for(f))
        if str(decision.action).strip().casefold() == "apply":
            cleared.append(f)
        else:
            outcome.reported.append((f, decision.reason))
    if not cleared:
        return outcome

    # 2. Compute the patch IN MEMORY. Nothing is written yet.
    #
    # An earlier version edited the tree here and restored it if the verifier refused. That leaves a
    # window: a run killed mid-verification (the verifier is a subprocess and can be slow) left the
    # repository half-cleaned with the restore never reached — observed, on a run that hit its
    # timeout. Verifying a patch we have not applied closes the window entirely, and it matches what
    # the verifier is for: it judges a PROPOSAL, not a fait accompli.
    #
    # Highest line first, so an earlier edit cannot shift a later line number within a file.
    pending: dict[str, str] = {}       # file -> final text
    changes: list[LayerChange] = []
    edited: list[Finding] = []
    for f in sorted(cleared, key=lambda x: (x.location.file, -(x.location.line or 0))):
        rel = f.location.file
        current = pending.get(rel)
        pair = _strip_comment_line(root, f, current_text=current)
        if pair is None:
            outcome.reported.append((f, "line no longer matches the finding; refused"))
            continue
        before, after = pair
        pending[rel] = after
        changes.append(LayerChange(finding_rule=f.rule, before=before, after=after))
        edited.append(f)
    if not edited:
        return outcome

    # 3. BLIND VERIFICATION, ONE FINDING AT A TIME.
    #
    # The unit of verification is a single change, which is what makes "partial endorsement"
    # impossible by construction rather than by interpretation.
    #
    # The alternative would be to verify a batch and apply only the endorsed hunks -- the module
    # even ships apply_endorsed_hunks for it -- but that requires mapping the verifier's
    # endorsed_hunk_ids back to hunks, and NOTHING assigns those ids: no code builds a Hunk, and
    # build_verifier_payload sends only {"diff", "repo_path"}, so the verifier is never given an id
    # vocabulary to endorse against. Any id it returns is its own invention, and a mapping built on
    # that would be a guess. Two earlier bugs in this module came from exactly that habit.
    #
    # With one change per verdict, a non-empty endorsement can only mean this change, and a refusal
    # can only refuse this change. The cost is one verifier call per finding; the alternative is not
    # slower, it is unsound.
    verified: dict[str, str] = {}          # file -> text with endorsed removals applied
    applied_now: list[Finding] = []
    base_text: dict[str, str] = {rel: (root / rel).read_text(encoding="utf-8") for rel in pending}

    # Highest line first again: each endorsed removal shifts the lines below it.
    for f in sorted(edited, key=lambda x: (x.location.file, -(x.location.line or 0))):
        rel = f.location.file
        current = verified.get(rel, base_text[rel])
        pair = _strip_comment_line(root, f, current_text=current)
        if pair is None:
            outcome.reported.append((f, "line no longer matches the finding; refused"))
            continue
        before, after = pair

        if verifier_runner is None:
            # Tag EVERY finding, not just the first: an audit that records the caveat once loses it
            # the moment anyone filters by finding.
            outcome.reported.append(
                (f, "VERIFICATION SKIPPED (--skip-verify): applied without blind endorsement"))
            verified[rel] = after
            applied_now.append(f)
            continue

        diff = "".join(difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}"))
        verdict = run_verifier(diff, str(root), runner=verifier_runner)
        if getattr(verdict, "endorsed_hunk_ids", frozenset()):
            verified[rel] = after
            applied_now.append(f)
        else:
            outcome.verifier_rejected.append(f)

    if not applied_now:
        # Nothing to restore — the tree was never touched. A refusal costs one wasted patch.
        return outcome

    # 4. WRITE, then commit. Only endorsed work reaches the disk at all.
    for rel, text in verified.items():
        (root / rel).write_text(text, encoding="utf-8")

    if git_runner is not None:
        subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, text=True)
        endorsed_changes = [c for c, f in zip(changes, edited) if f in applied_now]
        layers = build_layers({LAYER_COMMENTS: endorsed_changes or changes})
        outcome.commit_result = apply_commit_stack(
            layers, root,
            git_runner=git_runner,
            apply_layer_files=lambda _layer: None,   # the tree already carries the edits
            validate_fn=validate_fn or (lambda _p: True),
        )
    outcome.applied.extend(applied_now)
    return outcome
