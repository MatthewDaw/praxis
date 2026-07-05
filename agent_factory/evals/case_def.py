"""The eval-case schema and loader (a small echo of Praxis's ``eval_def``).

A case is authored as ``case.yaml``. ``component`` selects which factory piece the
case exercises (only ``plan_gate`` today); the component-specific ``input`` block
is parsed into that component's types by the runner. ``deterministic_checks`` are
references to callables in ``evals/checks.py``, each invoked with the component's
produced verdict plus the check's ``params``.

Meta-eval fields (built out by coverage/RED-proof units):
- ``rule_ids`` — the stable gate rule-IDs this case exercises (coverage matrix).
- ``red_proof`` — falsifiability evidence (a harvested event ref or a broken-gate
  fixture the case must fail against); ``None`` means undemonstrated.
- ``status`` — ``active`` cases lock the suite; ``proposed`` (harvested, unratified)
  cases are ignored for green-locking until a human promotes them.

Loader guard (U7): the deterministic suite must never go flaky, so a case whose
``input`` block carries a non-deterministic concern (sleep / timeout / latency /
timestamp / now / concurrency / retries) is rejected at discovery and pointed at a
separate ``stress/`` lane.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

#: Keys whose presence in a case ``input`` block marks it as non-deterministic. Matched
#: at key level with word boundaries (Python ``\b`` treats ``_`` as a word char), so a
#: benign key like ``now_admitted`` is NOT flagged while a bare ``now`` is. Conservative
#: and extensible — add a keyword here as new flaky shapes appear.
NONDETERMINISTIC_KEYS: tuple[str, ...] = (
    "sleep",
    "timeout",
    "latency",
    "timestamp",
    "now",
    "concurrency",
    "retries",
)

_NONDET_RES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (kw, re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE)) for kw in NONDETERMINISTIC_KEYS
)


@dataclass
class CheckRef:
    """Points at a callable in ``evals/checks.py`` (``"module:function"``)."""

    name: str
    ref: str
    params: dict = field(default_factory=dict)


@dataclass
class EvalCase:
    id: str
    component: str  # "plan_gate"
    input: dict = field(default_factory=dict)  # component-specific scenario
    deterministic_checks: list[CheckRef] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)  # gate rule-IDs this case exercises
    red_proof: dict | None = None  # falsifiability evidence (harvested ref / fixture)
    status: str = "active"  # "active" locks the suite; "proposed" is ignored until promoted
    source_dir: str | None = None

    @staticmethod
    def from_dict(data: dict, source_dir: str | None = None) -> "EvalCase":
        checks = [
            CheckRef(name=c["name"], ref=c["ref"], params=c.get("params", {}))
            for c in data.get("deterministic_checks", [])
        ]
        if not checks:
            raise ValueError(f"case {data.get('id')!r} has no deterministic_checks")
        _reject_nondeterministic_input(data.get("input", {}), case_id=data.get("id"))
        return EvalCase(
            id=data["id"],
            component=data["component"],
            input=data.get("input", {}),
            deterministic_checks=checks,
            rule_ids=list(data.get("rule_ids", [])),
            red_proof=data.get("red_proof"),
            status=data.get("status", "active"),
            source_dir=source_dir,
        )


def _flagged_keys(obj: object) -> list[str]:
    """Recursively collect ``input`` keys that match a non-deterministic keyword."""
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str):
                for kw, pattern in _NONDET_RES:
                    if pattern.search(key):
                        found.append(key)
                        break
            found.extend(_flagged_keys(value))
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            found.extend(_flagged_keys(item))
    return found


def _reject_nondeterministic_input(case_input: dict, *, case_id: object) -> None:
    """Raise if ``case_input`` encodes a non-deterministic concern (U7 loader guard)."""
    flagged = _flagged_keys(case_input)
    if flagged:
        offenders = ", ".join(sorted(set(flagged)))
        raise ValueError(
            f"case {case_id!r} has non-deterministic input key(s) [{offenders}]; "
            f"the deterministic suite forbids time/ordering/IO-shaped keys "
            f"({', '.join(NONDETERMINISTIC_KEYS)}). Move it to a 'stress/' lane instead."
        )


def load_case(case_path: Path) -> EvalCase:
    """Load a single ``case.yaml`` into an :class:`EvalCase`."""
    data = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    return EvalCase.from_dict(data, source_dir=str(case_path.parent))


def discover_cases(cases_root: Path) -> list[EvalCase]:
    """Load every ``case.yaml`` under ``cases_root`` (sorted by id)."""
    cases = [load_case(p) for p in sorted(cases_root.rglob("case.yaml"))]
    return sorted(cases, key=lambda c: c.id)
