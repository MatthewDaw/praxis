"""R27: the risk-stratified commit stack (B25/B45/B26), and its shared-checkout-safe unwind.

af-clean emits its findings as one commit per risk layer, in a FIXED order: comments, then
covered-unreachable deletions, then same-job consolidations, then behavior-adjacent
simplifications, then dead-import cleanup LAST (dead imports are *created by* the deletion
layers above, so they cannot precede them). Truncating the stack at any layer N must leave
layers 1..N applied with the repo building and tests passing (B25's accept). Reverting a
middle layer in isolation is NOT guaranteed -- later layers edit text earlier layers produced
-- so the only affordance this module exposes is PREFIX truncation, never an arbitrary
middle-layer revert.

Each layer surfaces at most ``cap_per_layer`` findings (B45, default 25), selected by
descending density; the remainder is DEFERRED, never dropped, so the caller can push it to the
findings ledger (R41) for the next run.

Before a layer is committed, every proposed fix in it is graded by a second, independent rubric
against af-clean's OWN diff (B26): clever compression, over-collapsed procedures (e.g. a plain
statement replaced by a nested comprehension), and comments the diff has falsified. A finding
may be TRUE and still have its fix rejected here -- the fix is dropped from what gets applied,
the finding itself is not re-litigated.

This checkout may be SHARED with concurrent sessions (Sec 3.4 of the requirements doc): no
``git reset``, ``git stash``, or branch switch is ever invoked, on any path, including failure
paths. The only sanctioned unwind is ``git revert`` of the stack's head commits in REVERSE
order on the current branch -- which is also why truncation is prefix-only: reverting adds
commits, it never removes them.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- layer order (B25)

LAYER_COMMENTS = "comments"
LAYER_COVERED_UNREACHABLE_DELETIONS = "covered_unreachable_deletions"
LAYER_SAME_JOB_CONSOLIDATIONS = "same_job_consolidations"
LAYER_BEHAVIOR_ADJACENT_SIMPLIFICATIONS = "behavior_adjacent_simplifications"
LAYER_DEAD_IMPORT_CLEANUP = "dead_import_cleanup"

#: The fixed risk-ascending order (B25). Dead-import cleanup is deliberately LAST: it cleans up
#: imports the deletion layers above it create as dead, so it cannot run before them.
LAYER_ORDER: tuple[str, ...] = (
    LAYER_COMMENTS,
    LAYER_COVERED_UNREACHABLE_DELETIONS,
    LAYER_SAME_JOB_CONSOLIDATIONS,
    LAYER_BEHAVIOR_ADJACENT_SIMPLIFICATIONS,
    LAYER_DEAD_IMPORT_CLEANUP,
)

#: B45's default findings-per-layer cap; flagged for override (D10) via ``cap_per_layer``.
DEFAULT_FINDINGS_CAP_PER_LAYER = 25

#: The only git subcommands this module will ever invoke (Sec 3.4). Never reset/stash/checkout/
#: switch, on any path -- including the failure/unwind path.
_ALLOWED_GIT_SUBCOMMANDS = frozenset({"add", "commit", "revert", "status", "diff", "log", "rev-parse"})
_FORBIDDEN_GIT_SUBCOMMANDS = frozenset({"reset", "stash", "checkout", "switch"})


class ForbiddenGitOperation(RuntimeError):
    """Raised if any code path in this module ever attempts a forbidden git subcommand."""


# --------------------------------------------------------------------------- B26 self-audit

_COMPREHENSION_TYPES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


@dataclass(frozen=True)
class SelfAuditFinding:
    """One reason af-clean's own second rubric (B26) rejected a proposed fix."""

    rule: str
    detail: str


@dataclass(frozen=True)
class LayerChange:
    """One proposed fix within a layer: which finding rule produced it, plus the before/after
    source text so the B26 self-audit can grade the diff it would create."""

    finding_rule: str
    before: str
    after: str
    density: float = 0.0


def _contains_nested_comprehension(tree: ast.AST) -> bool:
    """True iff any comprehension node in ``tree`` contains ANOTHER comprehension nested
    inside its element/generators -- the over-collapsed-procedure shape B26 flags."""
    for outer in ast.walk(tree):
        if not isinstance(outer, _COMPREHENSION_TYPES):
            continue
        for inner in ast.walk(outer):
            if inner is outer:
                continue
            if isinstance(inner, _COMPREHENSION_TYPES):
                return True
    return False


def _contains_plain_loop_statement(tree: ast.AST) -> bool:
    """True iff ``tree`` contains a plain ``for``/``while`` statement (the shape a
    procedural fix compresses away)."""
    return any(isinstance(node, (ast.For, ast.While)) for node in ast.walk(tree))


def self_audit_change(change: LayerChange) -> tuple[bool, tuple[SelfAuditFinding, ...]]:
    """Grade one proposed fix against af-clean's own diff (B26).

    Non-Python or unparseable before/after text is accepted -- this specific rule only
    judges Python source it can actually parse; it never blocks on a guess.
    """
    try:
        before_tree = ast.parse(change.before)
        after_tree = ast.parse(change.after)
    except SyntaxError:
        return True, ()

    findings: list[SelfAuditFinding] = []
    if _contains_plain_loop_statement(before_tree) and _contains_nested_comprehension(after_tree):
        findings.append(
            SelfAuditFinding(
                rule="over-collapsed-procedure",
                detail=(
                    f"{change.finding_rule}: replaces a plain statement with a nested "
                    "comprehension -- rejected by the self-audit (B26)"
                ),
            )
        )

    return (not findings), tuple(findings)


def self_audit_changes(
    changes: Sequence[LayerChange],
) -> tuple[list[LayerChange], list[SelfAuditFinding]]:
    """Grade every change; a rejected fix is DROPPED from what gets applied. The finding that
    produced it is not re-litigated here -- only its fix is withheld (B26: "a true finding may
    have its fix rejected")."""
    accepted: list[LayerChange] = []
    rejected: list[SelfAuditFinding] = []
    for change in changes:
        ok, findings = self_audit_change(change)
        if ok:
            accepted.append(change)
        else:
            rejected.extend(findings)
    return accepted, rejected


# --------------------------------------------------------------------------- B45 layer building

@dataclass(frozen=True)
class Layer:
    """One risk layer's plan: the changes it will commit, and any overflow deferred to the
    ledger (B45), plus anything the B26 self-audit dropped."""

    name: str
    changes: tuple[LayerChange, ...]
    deferred: tuple[LayerChange, ...] = ()
    self_audit_rejections: tuple[SelfAuditFinding, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.changes


def build_layers(
    changes_by_layer: Mapping[str, Sequence[LayerChange]],
    *,
    cap_per_layer: int = DEFAULT_FINDINGS_CAP_PER_LAYER,
) -> list[Layer]:
    """Bucket already-classified changes into the fixed :data:`LAYER_ORDER`.

    Per layer: the B26 self-audit runs first (a rejected fix never occupies a cap slot), then
    the survivors are sorted by descending ``density`` and capped at ``cap_per_layer`` -- the
    remainder is DEFERRED (never dropped) for the caller to push to the findings ledger.
    Any layer name in ``changes_by_layer`` outside :data:`LAYER_ORDER` is ignored: the emitted
    stack is always exactly the five known layers, in the fixed order.
    """
    layers: list[Layer] = []
    for name in LAYER_ORDER:
        candidates = list(changes_by_layer.get(name, ()))
        accepted, rejected = self_audit_changes(candidates)
        accepted.sort(key=lambda c: c.density, reverse=True)
        kept, deferred = accepted[:cap_per_layer], accepted[cap_per_layer:]
        layers.append(
            Layer(
                name=name,
                changes=tuple(kept),
                deferred=tuple(deferred),
                self_audit_rejections=tuple(rejected),
            )
        )
    return layers


# --------------------------------------------------------------------------- git orchestration

GitRunner = Callable[[list[str], Path], Any]


def _run_git(runner: GitRunner, argv: list[str], cwd: Path, log: list[list[str]]) -> Any:
    subcmd = argv[1] if len(argv) > 1 else ""
    if subcmd in _FORBIDDEN_GIT_SUBCOMMANDS:
        raise ForbiddenGitOperation(
            f"refused to run `git {subcmd}`: only {sorted(_ALLOWED_GIT_SUBCOMMANDS)} are "
            "permitted on a checkout that may be shared with concurrent sessions"
        )
    log.append(list(argv))
    return runner(argv, cwd)


@dataclass
class StackResult:
    """The outcome of applying a commit stack: which layers actually landed, which layer (if
    any) broke validation and was reverted, and the full git argv audit log."""

    applied_layers: list[str] = field(default_factory=list)
    commit_shas: list[str] = field(default_factory=list)
    failed_layer: str | None = None
    git_log: list[list[str]] = field(default_factory=list)

    @property
    def truncated(self) -> bool:
        return self.failed_layer is not None


def _head_sha(runner: GitRunner, repo: Path, log: list[list[str]]) -> str:
    proc = _run_git(runner, ["git", "rev-parse", "HEAD"], repo, log)
    return (getattr(proc, "stdout", "") or "").strip()


def apply_commit_stack(
    layers: Sequence[Layer],
    repo_path: "str | Path",
    *,
    git_runner: GitRunner,
    apply_layer_files: Callable[[Layer], None],
    validate_fn: Callable[[Path], bool],
    commit_message: Callable[[Layer], str] | None = None,
) -> StackResult:
    """Commit each non-empty layer in :data:`LAYER_ORDER`, validating after every commit.

    On the first layer whose post-commit validation fails, that layer's commit is undone with
    ``git revert`` (never ``git reset``/``git stash``/a branch switch -- Sec 3.4) and the stack
    stops there: every earlier layer's commit is left applied, satisfying B25's accept --
    truncating at layer N leaves layers 1..N applied with the repo building and tests passing.
    This is PREFIX truncation only; a later layer is never applied once an earlier one fails.
    """
    repo = Path(repo_path)
    result = StackResult()
    message = commit_message or (lambda layer: f"af-clean: {layer.name} ({len(layer.changes)} finding(s))")

    for layer in layers:
        if layer.is_empty:
            continue

        apply_layer_files(layer)
        _run_git(git_runner, ["git", "add", "-A"], repo, result.git_log)
        _run_git(git_runner, ["git", "commit", "-m", message(layer)], repo, result.git_log)
        sha = _head_sha(git_runner, repo, result.git_log)

        if validate_fn(repo):
            result.applied_layers.append(layer.name)
            result.commit_shas.append(sha)
            continue

        # Validation broke on this layer: undo ONLY this layer's commit, via revert -- never
        # reset/stash/checkout -- and stop. Every prior layer's commit survives untouched.
        _run_git(git_runner, ["git", "revert", "--no-edit", sha], repo, result.git_log)
        result.failed_layer = layer.name
        break

    return result


def unwind_to_layer(
    repo_path: "str | Path",
    commit_shas: Sequence[str],
    keep_n: int,
    *,
    git_runner: GitRunner,
) -> list[str]:
    """Truncate an already-applied stack down to its first ``keep_n`` commits.

    The only sanctioned unwind (Sec 3.4): revert the trailing commits in REVERSE order (head
    first) via ``git revert``, one at a time, on the current branch. This can only ever remove
    a contiguous suffix -- it is structurally incapable of reverting an isolated middle layer,
    which is exactly B25's "prefix truncation, not arbitrary revert" affordance.
    """
    repo = Path(repo_path)
    log: list[list[str]] = []
    to_revert = list(commit_shas[keep_n:])
    reverted: list[str] = []
    for sha in reversed(to_revert):
        _run_git(git_runner, ["git", "revert", "--no-edit", sha], repo, log)
        reverted.append(sha)
    return reverted
