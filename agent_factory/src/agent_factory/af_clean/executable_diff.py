"""Bounded, witnessed application of an explicit executable-code diff.

The ordinary af-clean applier intentionally edits comments only.  This module is the narrower
seam for a planned consolidation whose patch already exists: the caller must name the exact
finding instances, exact paths, and execution witnesses.  The patch is first applied in an
isolated clone, the witnesses run there, and a blind verifier sees only the diff, repository path,
and the finding's change class.  The real checkout is touched only after every gate passes.

No commit is made here.  Risk-layer selection and commit construction remain the caller's job.
"""

from __future__ import annotations

import shlex
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .applier import default_subprocess_runner
from .findings import Finding, admit_finding
from .verifier import run_verifier


class ExecutableDiffRefused(RuntimeError):
    """The proposed patch failed a safety, witness, or verification gate."""


@dataclass(frozen=True)
class WitnessCommand:
    """One tier-2 execution witness, run without a shell in the isolated clone."""

    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.argv or any(not isinstance(item, str) or not item for item in self.argv):
            raise ValueError("witness argv must contain non-empty strings")


@dataclass(frozen=True)
class ExecutableDiffResult:
    applied_paths: tuple[str, ...]
    witnesses_run: int
    change_class: str


CommandRunner = Callable[..., object]


def _run(runner: CommandRunner, argv: Sequence[str], *, cwd: Path,
         input_text: str | None = None) -> object:
    return runner(list(argv), cwd=cwd, input=input_text, capture_output=True, text=True)


def _require_success(result: object, label: str) -> None:
    if getattr(result, "returncode", 1) != 0:
        stderr = str(getattr(result, "stderr", "")).strip()
        stdout = str(getattr(result, "stdout", "")).strip()
        detail = stderr or stdout or "no output"
        raise ExecutableDiffRefused(f"{label} failed: {detail}")


def _diff_paths(diff: str) -> frozenset[str]:
    paths: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            raise ExecutableDiffRefused(f"malformed diff header: {line!r}") from exc
        if len(parts) != 4 or not parts[2].startswith("a/") or not parts[3].startswith("b/"):
            raise ExecutableDiffRefused(f"unsupported diff header: {line!r}")
        before, after = parts[2][2:], parts[3][2:]
        if before != after:
            raise ExecutableDiffRefused("renames are outside the bounded executable-diff adapter")
        paths.add(after)
    if not paths:
        raise ExecutableDiffRefused("prebuilt diff contains no git diff paths")
    return frozenset(paths)


def _dirty_paths(repo_root: Path, runner: CommandRunner) -> frozenset[str]:
    result = _run(
        runner,
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo_root,
    )
    _require_success(result, "git status")
    paths: set[str] = set()
    for line in str(getattr(result, "stdout", "")).splitlines():
        if not line:
            continue
        value = line[3:]
        if " -> " in value:
            old, new = value.split(" -> ", 1)
            paths.update((old, new))
        else:
            paths.add(value)
    return frozenset(paths)


def apply_bounded_executable_diff(
    repo_root: str | Path,
    diff: str,
    findings: Sequence[Finding],
    *,
    expected_rule: str,
    expected_locations: frozenset[tuple[str, int]],
    diff_allowlist: frozenset[str],
    witnesses: Sequence[WitnessCommand],
    change_class: str,
    command_runner: CommandRunner = subprocess.run,
    verifier_runner: CommandRunner = default_subprocess_runner,
) -> ExecutableDiffResult:
    """Apply one fully bounded patch after admission, execution witnesses, and blind review.

    The real repository must be clean.  This is stronger than tolerating expected dirt: a patch
    that already exists in the checkout was written before verification, defeating the seam's
    central safety property.
    """
    root = Path(repo_root).resolve()
    if not (root / ".git").exists():
        raise ExecutableDiffRefused(f"not a git repository: {root}")
    if not diff.strip():
        raise ExecutableDiffRefused("prebuilt diff is empty")
    if not witnesses:
        raise ExecutableDiffRefused("tier-2 execution witness commands are required")
    if verifier_runner is None:
        raise ExecutableDiffRefused("blind verification cannot be skipped")

    actual_locations: list[tuple[str, int]] = []
    for finding in findings:
        verdict = admit_finding(finding)
        if not verdict.admitted:
            raise ExecutableDiffRefused(f"finding was not admitted: {verdict.reason}")
        if finding.rule != expected_rule:
            raise ExecutableDiffRefused(
                f"unexpected finding rule {finding.rule!r}; expected {expected_rule!r}"
            )
        if finding.change_class != change_class:
            raise ExecutableDiffRefused(
                f"finding change class {finding.change_class!r} != {change_class!r}"
            )
        assert finding.location is not None and finding.location.line is not None
        actual_locations.append((finding.location.file, finding.location.line))
    if len(actual_locations) != len(expected_locations) or set(actual_locations) != set(expected_locations):
        raise ExecutableDiffRefused(
            f"finding locations/count differ: expected {sorted(expected_locations)!r}, "
            f"got {sorted(actual_locations)!r}"
        )

    paths = _diff_paths(diff)
    if paths != diff_allowlist:
        raise ExecutableDiffRefused(
            f"diff paths differ from exact allowlist: expected {sorted(diff_allowlist)!r}, "
            f"got {sorted(paths)!r}"
        )
    dirt = _dirty_paths(root, command_runner)
    if dirt:
        raise ExecutableDiffRefused(f"repository has unexpected dirty paths: {sorted(dirt)!r}")

    head_result = _run(command_runner, ["git", "rev-parse", "HEAD"], cwd=root)
    _require_success(head_result, "resolve HEAD")
    original_head = str(getattr(head_result, "stdout", "")).strip()

    with tempfile.TemporaryDirectory(prefix="af-clean-executable-") as temporary:
        clone = Path(temporary) / "repo"
        clone_result = _run(
            command_runner,
            ["git", "clone", "--quiet", "--no-local", str(root), str(clone)],
            cwd=root,
        )
        _require_success(clone_result, "isolated witness clone")
        apply_result = _run(command_runner, ["git", "apply", "--whitespace=error-all", "-"],
                            cwd=clone, input_text=diff)
        _require_success(apply_result, "apply proposed diff in witness clone")
        for index, witness in enumerate(witnesses, 1):
            result = _run(command_runner, witness.argv, cwd=clone)
            _require_success(result, f"tier-2 witness {index} ({' '.join(witness.argv)})")

    # Role separation is structural in run_verifier: findings and witness output are not passed.
    verifier_verdict = run_verifier(
        diff,
        str(root),
        change_class=change_class,
        runner=verifier_runner,
    )
    if verifier_verdict.endorsed_hunk_ids != frozenset({"h1"}):
        raise ExecutableDiffRefused(
            "blind verifier did not affirmatively endorse the bounded patch as h1"
        )

    # Close the race between witnessing/verifying and applying to the real checkout.
    if _dirty_paths(root, command_runner):
        raise ExecutableDiffRefused("repository became dirty before endorsed patch application")
    current_head = _run(command_runner, ["git", "rev-parse", "HEAD"], cwd=root)
    _require_success(current_head, "re-resolve HEAD")
    if str(getattr(current_head, "stdout", "")).strip() != original_head:
        raise ExecutableDiffRefused("HEAD changed during witnessed patch verification")

    check_result = _run(command_runner, ["git", "apply", "--check", "--whitespace=error-all", "-"],
                        cwd=root, input_text=diff)
    _require_success(check_result, "final git apply check")
    real_apply = _run(command_runner, ["git", "apply", "--whitespace=error-all", "-"],
                      cwd=root, input_text=diff)
    _require_success(real_apply, "apply endorsed patch")
    return ExecutableDiffResult(tuple(sorted(paths)), len(witnesses), change_class)
