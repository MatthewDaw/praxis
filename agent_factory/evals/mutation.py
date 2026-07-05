"""Mutation-testing worklist wrapper (U5; R8, SC1, AE3) — report-only (KTD7).

``mutmut`` mutates the gate logic + checks (see ``[tool.mutmut]`` in ``pyproject.toml``:
``plan_gate.py``, ``gate.py``, ``evals/checks.py``) and runs the eval suite against each
mutant. A mutant the suite still passes is a *surviving* mutant — proof of an adequacy
hole: some rule edit the cases cannot tell from the original. This module turns the raw
``mutmut results`` text into a structured **wanted-case worklist** (one entry per
survivor: where it lives + what to do) and a **kill-rate** report metric.

It is deliberately *report-first* (KTD7): nothing here blocks CI, and mutation never runs
inside the default ``pytest`` invocation. The flow is out-of-band::

    python -m mutmut run        # mutate the configured modules, run the suite per mutant
    python -m mutmut results    # the survivor listing this module parses

Why parse rather than gate: mutmut on Windows is flaky, so the kill-rate is a tracked
metric (target :data:`TARGET_KILL_RATE`), not a merge gate — survivors are recorded as a
worklist of cases still wanted (e.g. AE3: a mutant that forces ``R-NO-DANGLING`` to always
pass should be *killed* by the dangling case; if it survives, it lands here).

Format note: this parses the ``mutmut`` **2.x** ``results`` layout — status sections
(``Survived 🙁 (N)``), each split into ``---- <file> (n) ----`` blocks whose body is a
comma/range list of mutant IDs (``12-13, 15``). ``mutmut results`` lists only the
*not-killed* buckets (survived / timed-out / suspicious / skipped); killed mutants are not
echoed, so the total needed for the kill-rate is supplied by the caller (the ``mutmut run``
summary reports it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Target suite kill-rate, reported (not enforced — KTD7). Surfaced so a human reading the
#: worklist knows whether the suite clears the bar; SC1 is "no surviving mutant on any
#: shipped rule", of which this 80% line is the softer, always-reported proxy.
TARGET_KILL_RATE: float = 0.80

# A status section header, e.g. ``Survived 🙁 (3)`` or ``Timed out ⏰ (1)``. The trailing
# ``(N)`` count is mutmut's own tally for that bucket.
_STATUS_RE = re.compile(
    r"^(Survived|Timed out|Timeout|Suspicious|Untested/skipped|Untested|Skipped)\b"
    r"[^(]*\((\d+)\)\s*$"
)
# A per-file sub-header inside a status section: ``---- evals/checks.py (1) ----``.
_FILE_RE = re.compile(r"^-{2,}\s*(?P<path>.+?)\s*\(\d+\)\s*-{2,}\s*$")
# A mutant-ID body line: only digits, commas, hyphens, whitespace (``12-13, 15``).
_IDS_RE = re.compile(r"^[\d][\d,\s-]*$")

# mutmut's bucket labels -> a stable canonical key used in :attr:`MutationReport.counts`.
_STATUS_CANON = {
    "Survived": "survived",
    "Timed out": "timeout",
    "Timeout": "timeout",
    "Suspicious": "suspicious",
    "Untested/skipped": "skipped",
    "Untested": "skipped",
    "Skipped": "skipped",
}


@dataclass(frozen=True)
class SurvivingMutant:
    """One mutant the suite failed to kill — a single wanted-case worklist entry.

    ``mutant_id`` is mutmut's id (feed it to ``mutmut show <id>`` to inspect the exact
    source mutation); ``location`` is the file it lives in. ``description`` is the
    ready-to-print worklist line telling a human what case is still wanted.
    """

    mutant_id: str
    location: str
    description: str


@dataclass
class MutationReport:
    """Structured summary of one ``mutmut results`` run (report-only — KTD7).

    ``survived`` is the wanted-case worklist (escapes the suite did not catch). ``counts``
    maps each non-killed bucket (``survived`` / ``timeout`` / ``suspicious`` / ``skipped``)
    to mutmut's reported tally. Killed mutants are not in ``mutmut results``, so the
    kill-rate needs a caller-supplied ``total`` (from the ``mutmut run`` summary).
    """

    survived: list[SurvivingMutant] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def worklist(self) -> list[SurvivingMutant]:
        """The wanted-case worklist — every surviving mutant, in listed order."""
        return self.survived

    @property
    def survivor_count(self) -> int:
        """How many mutants survived (the size of the worklist)."""
        return len(self.survived)

    def kill_rate(self, total_mutants: int) -> float:
        """Fraction of mutants killed: ``(total - survivors) / total``.

        Only *survived* mutants count as escapes (timed-out / suspicious mutants were
        caught, just noisily); ``total_mutants`` is the full count mutmut generated, taken
        from the ``mutmut run`` summary. Raises ``ValueError`` if ``total_mutants <= 0``.
        """
        if total_mutants <= 0:
            raise ValueError(f"total_mutants must be positive, got {total_mutants}")
        return (total_mutants - self.survivor_count) / total_mutants

    def meets_target(self, total_mutants: int) -> bool:
        """Whether the kill-rate clears :data:`TARGET_KILL_RATE` (report metric, not a gate)."""
        return self.kill_rate(total_mutants) >= TARGET_KILL_RATE


def _expand_ids(spec: str) -> list[str]:
    """Expand a mutmut id list like ``"12-13, 15"`` into ``["12", "13", "15"]``."""
    ids: list[str] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo, hi = (part.strip() for part in chunk.split("-", 1))
            ids.extend(str(i) for i in range(int(lo), int(hi) + 1))
        else:
            ids.append(chunk)
    return ids


def _describe(mutant_id: str, location: str) -> str:
    """The worklist line for one survivor: where it is + the next action."""
    return (
        f"{location}: mutant #{mutant_id} survived — add or strengthen a case that "
        f"distinguishes this mutation (inspect with `mutmut show {mutant_id}`)"
    )


def parse_results(results_text: str) -> MutationReport:
    """Parse ``mutmut results`` (2.x) output into a :class:`MutationReport`.

    Walks the status sections; collects mutant IDs under the *Survived* bucket (grouped by
    file) into the worklist, and records every non-killed bucket's count. Non-survivor
    sections (timed-out / suspicious / skipped) contribute to ``counts`` but not to the
    worklist. Unrecognized lines (the ``mutmut apply``/``show`` preamble, blanks) are
    ignored, so partial or empty input yields an empty report rather than raising.
    """
    report = MutationReport()
    current_status: str | None = None
    current_file: str | None = None

    for raw in results_text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue

        status_m = _STATUS_RE.match(line)
        if status_m:
            current_status = _STATUS_CANON[status_m.group(1)]
            report.counts[current_status] = int(status_m.group(2))
            current_file = None
            continue

        file_m = _FILE_RE.match(line)
        if file_m:
            current_file = file_m.group("path")
            continue

        if current_status == "survived" and current_file and _IDS_RE.match(line.strip()):
            for mutant_id in _expand_ids(line.strip()):
                report.survived.append(
                    SurvivingMutant(
                        mutant_id=mutant_id,
                        location=current_file,
                        description=_describe(mutant_id, current_file),
                    )
                )

    return report


def render_worklist(report: MutationReport, total_mutants: int | None = None) -> str:
    """Render the worklist as a human-readable report block (report-only — KTD7).

    Leads with the kill-rate vs :data:`TARGET_KILL_RATE` when ``total_mutants`` is known,
    then lists each surviving mutant as a wanted case. A clean suite (no survivors) renders
    an explicit "no surviving mutants" line so a green run is not mistaken for no-data.
    """
    lines: list[str] = ["Mutation worklist (report-only — KTD7)"]
    if total_mutants is not None:
        rate = report.kill_rate(total_mutants)
        flag = "OK" if rate >= TARGET_KILL_RATE else "BELOW TARGET"
        lines.append(
            f"kill-rate: {rate:.0%} ({total_mutants - report.survivor_count}"
            f"/{total_mutants} killed; target {TARGET_KILL_RATE:.0%}) [{flag}]"
        )
    if not report.survived:
        lines.append("no surviving mutants — every mutant was killed by the suite")
        return "\n".join(lines)
    lines.append(f"{report.survivor_count} surviving mutant(s) — wanted cases:")
    lines.extend(f"  - {m.description}" for m in report.survived)
    return "\n".join(lines)
