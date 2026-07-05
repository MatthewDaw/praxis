"""The rule x case coverage matrix — the meta-eval that green-locks the gate suite.

A gate rule that no case exercises is an untested promise; the suite can stay green
while the rule silently rots. This module builds a matrix of the ``plan_gate`` rule-IDs
(the stable constants the gate emits, KTD5) against the discovered eval cases, then
exposes the holes:

- :meth:`CoverageMatrix.uncovered_rules` — shipped rules with zero exercising case. A
  meta-test asserts this is empty; a new rule with no case turns CI red, naming the rule
  (SC2, AE1).
- :meth:`CoverageMatrix.rules_without_red_case` — rules whose exercising cases all lack a
  ``red_proof`` field. This reads FIELD PRESENCE only (``red_proof is not None``), never
  the falsifiability evidence itself, so coverage does not import ``red_proof.py`` (the
  deep verifier composes on top of this signal in U3).
- :meth:`CoverageMatrix.dangling_tags` — cases tagging a rule-ID that no gate defines (a
  typo'd ``rule_ids`` entry that would otherwise claim phantom coverage).

A case ``exercises`` a rule when its ``rule_ids`` lists that rule. Only ``active`` cases
count toward coverage; ``proposed`` (harvested, unratified) cases are ignored for
green-locking, matching the suite-lock rule in :mod:`evals.case_def`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_factory.plan_gate import R_ACCEPT_BINARY, R_NO_DANGLING, R_NO_VAGUE

from evals.case_def import EvalCase, discover_cases

#: The gate rule-IDs the factory ships today (the ``plan_gate`` constants). Coverage is
#: computed against this set; adding a rule here without a case turns the meta-test red.
SHIPPED_RULE_IDS: tuple[str, ...] = (R_ACCEPT_BINARY, R_NO_VAGUE, R_NO_DANGLING)

#: Cell glyphs for :meth:`CoverageMatrix.render`; a hole must read differently from a
#: covered cell so a rendered matrix shows gaps at a glance.
COVERED_CELL = "X"
HOLE_CELL = "."

#: Default location of the authored case suite (``evals/cases/<name>/case.yaml``).
_CASES_ROOT = Path(__file__).resolve().parent / "cases"


def shipped_rule_ids() -> tuple[str, ...]:
    """The shipped gate rule-IDs (a copy of :data:`SHIPPED_RULE_IDS`)."""
    return tuple(SHIPPED_RULE_IDS)


def default_cases() -> list[EvalCase]:
    """Discover the authored cases under ``evals/cases`` (sorted by id)."""
    return discover_cases(_CASES_ROOT)


@dataclass(frozen=True)
class CoverageMatrix:
    """A rule-ID x case matrix over a fixed rule set and a fixed list of cases.

    ``rules`` are the rule-IDs whose coverage is being audited; ``cases`` are the eval
    cases that may exercise them. The matrix derives every coverage signal lazily from
    these two — it holds no mutable state — so the same object answers ``uncovered_rules``,
    ``rules_without_red_case``, ``dangling_tags``, and :meth:`render` consistently.
    """

    rules: tuple[str, ...]
    cases: tuple[EvalCase, ...]

    def cases_for(self, rule_id: str, *, include_proposed: bool = False) -> list[EvalCase]:
        """Cases that exercise ``rule_id`` (``active`` only unless ``include_proposed``)."""
        return [
            c
            for c in self.cases
            if rule_id in c.rule_ids and (include_proposed or c.status == "active")
        ]

    def is_covered(self, rule_id: str) -> bool:
        """True iff at least one active case exercises ``rule_id``."""
        return bool(self.cases_for(rule_id))

    def uncovered_rules(self) -> list[str]:
        """Shipped rules with zero exercising active case, in matrix order (SC2/AE1)."""
        return [r for r in self.rules if not self.is_covered(r)]

    def rules_without_red_case(self) -> list[str]:
        """Rules whose exercising cases all lack a ``red_proof`` field.

        Presence test only (``red_proof is not None``): this never inspects what the
        evidence *is* (that is :mod:`evals.red_proof`'s job), so coverage stays free of a
        dependency on the RED-proof verifier. A rule with no exercising case is included
        (it trivially has no RED-proven case).
        """
        return [
            r
            for r in self.rules
            if not any(c.red_proof is not None for c in self.cases_for(r))
        ]

    def dangling_tags(self) -> dict[str, list[str]]:
        """Map each case id to the rule-IDs it tags that no rule in ``rules`` defines.

        A dangling tag is a typo'd or stale ``rule_ids`` entry; it must be flagged because
        it would otherwise let a case claim coverage of a rule that does not exist.
        """
        known = set(self.rules)
        flagged: dict[str, list[str]] = {}
        for c in self.cases:
            bad = [rid for rid in c.rule_ids if rid not in known]
            if bad:
                flagged[c.id] = bad
        return flagged

    def render(self) -> str:
        """Render the matrix as text, with holes (``.``) distinct from covered (``X``).

        One row per rule, one trailing column per exercising-case count, plus a legend and
        a ``HOLES`` line naming any uncovered rule so a rendered report points at the gap.
        """
        width = max((len(r) for r in self.rules), default=4)
        lines = [f"coverage matrix ({COVERED_CELL}=covered, {HOLE_CELL}=hole)"]
        for rule in self.rules:
            covering = [c.id for c in self.cases_for(rule)]
            cell = COVERED_CELL if covering else HOLE_CELL
            detail = ", ".join(covering) if covering else "(no case)"
            lines.append(f"  {rule.ljust(width)}  {cell}  {detail}")
        holes = self.uncovered_rules()
        dangling = self.dangling_tags()
        lines.append(f"HOLES: {', '.join(holes) if holes else '(none)'}")
        if dangling:
            pairs = "; ".join(f"{cid} -> {tags}" for cid, tags in dangling.items())
            lines.append(f"DANGLING TAGS: {pairs}")
        return "\n".join(lines)


def build_matrix(
    rules: tuple[str, ...] | list[str] | None = None,
    cases: list[EvalCase] | tuple[EvalCase, ...] | None = None,
) -> CoverageMatrix:
    """Build a :class:`CoverageMatrix` (defaults: shipped rules x discovered cases)."""
    chosen_rules = tuple(rules) if rules is not None else shipped_rule_ids()
    chosen_cases = tuple(cases) if cases is not None else tuple(default_cases())
    return CoverageMatrix(rules=chosen_rules, cases=chosen_cases)


def uncovered_rules(
    rules: tuple[str, ...] | list[str] | None = None,
    cases: list[EvalCase] | None = None,
) -> list[str]:
    """Convenience: shipped rules with no exercising case (see the method)."""
    return build_matrix(rules, cases).uncovered_rules()


def rules_without_red_case(
    rules: tuple[str, ...] | list[str] | None = None,
    cases: list[EvalCase] | None = None,
) -> list[str]:
    """Convenience: rules whose cases all lack a ``red_proof`` field (see the method)."""
    return build_matrix(rules, cases).rules_without_red_case()


def render_matrix(
    rules: tuple[str, ...] | list[str] | None = None,
    cases: list[EvalCase] | None = None,
) -> str:
    """Convenience: render the default (or supplied) matrix to text."""
    return build_matrix(rules, cases).render()
