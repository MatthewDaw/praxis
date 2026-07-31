"""R50: af-clean's two entry points over one engine.

Two callers hit the same finding-production + admission machinery
(:mod:`agent_factory.af_clean.findings`) from two different directions, and the two are never
collapsed into one code path:

* **E1 — human-invoked, ``/af-clean [path...]``.** Whole-repo by default, or a caller-named
  subtree. Runs against arbitrary repositories, most of which never went through ``af-build``.
  After producing findings it invokes ``the validation step``
  (:func:`agent_factory.af_clean_validate.run_validation_and_remediation`) ITSELF — E1 is the only
  entry point that does, because nothing else in an ad-hoc repo run will. It returns findings
  ADVISORILY: :class:`E1Result` carries findings + the validation report, never a top-level
  pass/fail verdict field, because there is no ticket for a verdict to gate.
* **E2 — axis-invoked, ``minimalism-dry``'s remediation arm on graded failure.** Fired inside an
  ``af-build`` ticket, scoped to that ticket's diff ONLY (:func:`run_e2` takes the diff text, never
  a repo path). It does NOT invoke the validation step inline — ``af-build`` already validates at
  end of run — and it ships report-only (D8): every finding, ``enforce`` or ``advise``, is
  returned as a REPORT, never auto-applied, until calibration lands the D8 flip. This trivially
  satisfies "applies no advise-tier finding" by applying nothing at all.

Both entry points share the same **dry-run** contract: ``dry_run=True`` on E1 skips the apply step
but produces the EXACT SAME findings a live run would — dry-run changes nothing about what is
found, only whether anything is written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from .findings import Finding, admit_finding
from ..af_clean_validate import (
    CommandRunner,
    ValidateRemediateReport,
    default_runner,
    run_validation_and_remediation,
)

# A repo-scoped finding producer: given the resolved scope path, returns candidate findings
# (pre-admission) for E1.
FindingProducer = Callable[[Path], Sequence[Finding]]

# A diff-scoped finding producer: given ONLY the ticket diff text, returns candidate findings
# (pre-admission) for E2. Never receives a repo path — E2's whole point is that it cannot reach
# outside the ticket's own diff.
DiffFindingProducer = Callable[[str], Sequence[Finding]]

ApplyFindings = Callable[[Sequence[Finding]], None]


def resolve_scope(repo_root: "str | Path", path: "str | None" = None) -> Path:
    """Resolve ``/af-clean [path...]``'s scope: the repo root by default, or the caller-named
    subtree. A relative ``path`` is resolved against ``repo_root``; an absolute one is used as-is.
    No argument (``path`` falsy) always scopes to ``repo_root`` itself.
    """
    root = Path(repo_root)
    if not path:
        return root
    p = Path(path)
    return p if p.is_absolute() else root / p


def _admitted_findings(candidates: Sequence[Finding]) -> tuple[Finding, ...]:
    return tuple(f for f in candidates if admit_finding(f).admitted)


# --------------------------------------------------------------------------- E1 — human-invoked

@dataclass
class E1Result:
    """E1's output: a scope, the admitted findings, and the validation step's own report.

    Deliberately carries NO top-level pass/fail/verdict field — findings are advisory, and the
    validation report's :attr:`ValidateRemediateReport.overall_status` is informational detail
    about the validation step itself, not an af-clean go/no-go call on the run.
    """

    scope: Path
    findings: tuple[Finding, ...]
    validation: ValidateRemediateReport
    applied: bool = False


def run_e1(
    repo_root: "str | Path",
    path: "str | None" = None,
    *,
    produce_findings: FindingProducer,
    apply_findings: Optional[ApplyFindings] = None,
    dry_run: bool = False,
    runner: CommandRunner = default_runner,
    validate_kwargs: Optional[dict] = None,
) -> E1Result:
    """Run E1: resolve scope, produce + admit findings, apply them unless ``dry_run``, then
    invoke the validation step over the whole repo and return everything advisorily.

    ``dry_run=True`` never calls ``apply_findings`` — the run still produces the identical
    findings a live run would, it just writes nothing.
    """
    scope = resolve_scope(repo_root, path)
    findings = _admitted_findings(produce_findings(scope))

    applied = False
    if not dry_run and apply_findings is not None and findings:
        apply_findings(findings)
        applied = True

    report = run_validation_and_remediation(repo_root, runner=runner, **(validate_kwargs or {}))

    return E1Result(scope=scope, findings=findings, validation=report, applied=applied)


# --------------------------------------------------------------------------- E2 — axis-invoked

@dataclass
class E2Result:
    """E2's output: every admitted finding from the ticket diff, all of it held at ``report`` —
    E2 ships report-only (D8), so :attr:`applied` is always empty regardless of tier or witness
    evidence. No validation step is ever invoked here (that field simply does not exist on this
    result — ``af-build`` validates at end of run instead)."""

    findings: tuple[Finding, ...]
    applied: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def reported(self) -> tuple[Finding, ...]:
        return self.findings


def run_e2(
    ticket_diff: str,
    *,
    produce_findings: DiffFindingProducer,
) -> E2Result:
    """Run E2: findings produced from the ticket diff ONLY, admitted, and returned report-only.

    Takes no repo path and no ``dry_run`` (there is nothing to apply either way): E2 never calls
    the validation step and never applies a finding of any tier, so a caller cannot accidentally
    widen its scope past the diff it was handed or land an unattended edit.
    """
    findings = _admitted_findings(produce_findings(ticket_diff))
    return E2Result(findings=findings, applied=())
