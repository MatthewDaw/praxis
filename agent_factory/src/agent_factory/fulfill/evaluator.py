"""U2 — the deterministic calculation-graph evaluator.

This is the GUARDRAIL the LLM is never allowed inside (KTD3, S2/S10): it executes ``compute.yaml``
over ``rules.yaml`` in plain Python so the bottom line is reproducible and auditable. The op set is
CLOSED and the graph is ACYCLIC — it is a graph, not a language.

Three modes (D8):

- ``final``       — every required input is a recorded fact; a missing required input yields an
                    ``unknown`` line (null), never a crash.
- ``provisional`` — missing required inputs are filled from ``policy.defaults`` and marked
                    ``assumed``; the rest are ``known``. This is what the S4 ranking runs.
- ``what_if``     — a provisional run with an ``overlay`` of hypothetical field values on top, so
                    the loop can ask "what does the bottom line do if filing_status were X".

Each line carries a per-line **basis** ∈ {``known``, ``assumed``, ``unknown``} propagated through the
graph: any ``unknown`` input ⇒ ``unknown``; else any ``assumed`` ⇒ ``assumed``; else ``known``. The
oracle test pins the numbers byte-equal to ``../agent_tax_harness/app/tax_engine.py`` (KTD8).
"""

from __future__ import annotations

import math
from typing import Any

from .domain import ComputeStep, Domain

KNOWN = "known"
ASSUMED = "assumed"
UNKNOWN = "unknown"

# basis precedence: the "worst" (most uncertain) input dominates the result.
_BASIS_RANK = {KNOWN: 0, ASSUMED: 1, UNKNOWN: 2}
_RANK_BASIS = {0: KNOWN, 1: ASSUMED, 2: UNKNOWN}


def _combine_basis(*bases: str) -> str:
    """The result basis is the most-uncertain of its inputs (unknown > assumed > known)."""
    worst = max((_BASIS_RANK.get(b, 2) for b in bases), default=0)
    return _RANK_BASIS[worst]


def _defaults(domain: Domain) -> dict[str, Any]:
    """field -> default value from ``policy.defaults`` (each entry is ``{value, justification}``)."""
    out: dict[str, Any] = {}
    for name, spec in (domain.policy.get("defaults") or {}).items():
        if isinstance(spec, dict) and "value" in spec:
            out[name] = spec["value"]
    return out


def _to_bound(x: Any) -> float:
    """Coerce a bracket upper bound to a float. YAML's bare ``inf`` parses as the string "inf"."""
    if isinstance(x, str) and x.strip().lower().lstrip(".") in ("inf", "infinity"):
        return math.inf
    return float(x)


def marginal_tax(income: float, rows: list[list[Any]]) -> float:
    """Tax on ``income`` under a marginal bracket schedule (rows of ``[upper_bound, rate]``).

    Mirrors ``app/tax_engine.py:compute_tax`` exactly: each rate applies only to the slice of income
    within its band; the final row's upper bound is ``inf``. NOT rounded here — the ``round`` post-op
    on the compute step owns whole-dollar rounding, matching the harness's single ``round(tax)``.
    """
    income = max(0.0, income)
    tax = 0.0
    lower = 0.0
    for upper_raw, rate in rows:
        if income <= lower:
            break
        upper = _to_bound(upper_raw)
        slice_top = min(income, upper)
        tax += (slice_top - lower) * float(rate)
        lower = upper
    return tax


class _Run:
    """One evaluation pass. Holds the resolved field inputs + the accumulating step results."""

    def __init__(self, domain: Domain, facts: dict, mode: str, overlay: dict | None):
        self.domain = domain
        self.facts = facts or {}
        self.mode = mode
        self.overlay = overlay or {}
        self.defaults = _defaults(domain)
        self.results: dict[str, dict[str, Any]] = {}

    # --- field inputs -------------------------------------------------------
    def field(self, name: str) -> tuple[Any, str]:
        """Resolve an input field to ``(value, basis)`` honoring overlay / facts / defaults / mode."""
        if name in self.overlay and self.overlay[name] is not None:
            return self.overlay[name], KNOWN
        if name in self.facts and self.facts[name] is not None:
            return self.facts[name], KNOWN
        if self.mode in ("provisional", "what_if") and name in self.defaults:
            return self.defaults[name], ASSUMED
        return None, UNKNOWN

    def field_sum(self, name: str) -> tuple[Any, str]:
        """Sum a field across all gathered facts (a field may hold a scalar or a list of W-2 values)."""
        value, basis = self.field(name)
        if value is None:
            return None, basis
        if isinstance(value, (list, tuple)):
            nums = [v for v in value if v is not None]
            if not nums:
                return None, UNKNOWN
            return sum(float(v) for v in nums), basis
        return float(value), basis

    # --- operands -----------------------------------------------------------
    def operand(self, token: Any) -> tuple[Any, str]:
        """Resolve one operand to ``(value, basis)``: a step id, a ``{field:..}`` / ``{const:..}``
        mapping, a ``"field:<name>"`` string, or a bare number."""
        if isinstance(token, dict):
            if "field" in token:
                return self.field(str(token["field"]))
            if "const" in token:
                return token["const"], KNOWN
            raise EvaluatorError(f"unrecognized operand mapping {token!r}")
        if isinstance(token, (int, float)):
            return float(token), KNOWN
        if isinstance(token, str):
            if token.startswith("field:"):
                return self.field(token[len("field:"):])
            if token in self.results:
                r = self.results[token]
                return r["value"], r["basis"]
            raise EvaluatorError(f"operand {token!r} is not a known step id or field reference")
        raise EvaluatorError(f"unsupported operand {token!r}")


class EvaluatorError(ValueError):
    """A compute step is malformed in a way load-time validation did not catch."""


def _apply_post(value: Any, post: list[dict]) -> Any:
    """Apply the ordered post-ops (``clamp_min`` / ``round``). A null (unknown) value passes through
    untouched so an unknown line stays unknown."""
    if value is None:
        return None
    for op in post:
        for name, arg in op.items():
            if name == "clamp_min":
                value = max(float(arg), float(value))
            elif name == "round":
                step = float(arg) or 1.0
                value = round(float(value) / step) * step
            else:  # pragma: no cover - load-time validation rejects unknown post-ops
                raise EvaluatorError(f"unknown post-op {name!r}")
    return value


def _eval_step(run: _Run, step: ComputeStep) -> dict[str, Any]:
    """Execute one compute step, returning ``{"value":..., "basis":...}``."""
    op = step.op
    spec = step.spec
    value: Any
    basis: str

    if op == "sum":
        field_name = str(spec.get("field"))
        value, basis = run.field_sum(field_name)

    elif op == "copy":
        value, basis = run.operand(spec.get("from"))

    elif op == "const":
        value, basis = float(spec.get("value")), KNOWN

    elif op in ("add", "subtract"):
        parts = [run.operand(tok) for tok in (spec.get("inputs") or [])]
        bases = [b for _v, b in parts]
        if any(v is None for v, _b in parts):
            value, basis = None, _combine_basis(*bases)
        elif op == "add":
            value, basis = sum(float(v) for v, _b in parts), _combine_basis(*bases)
        else:  # subtract: first minus the rest
            head, *rest = parts
            value = float(head[0]) - sum(float(v) for v, _b in rest)
            basis = _combine_basis(*bases)

    elif op == "table_lookup":
        table = run.domain.rules.get(str(spec.get("table"))) or {}
        key_value, key_basis = run.operand(spec.get("key"))
        if key_value is None or key_value not in table:
            value, basis = None, (UNKNOWN if key_value is None else key_basis)
        else:
            value, basis = float(table[key_value]), key_basis

    elif op == "marginal_tax":
        income_value, income_basis = run.operand(spec.get("income"))
        schedule = run.domain.rules.get(str(spec.get("schedule"))) or {}
        key_value, key_basis = run.operand(spec.get("key"))
        rows = schedule.get(key_value) if key_value is not None else None
        if income_value is None or not rows:
            # key_basis is already UNKNOWN when the key is missing, so this propagates the
            # most-uncertain input (an unknown filing status -> an unknown tax line).
            value = None
            basis = _combine_basis(income_basis, key_basis)
        else:
            value = marginal_tax(float(income_value), rows)
            basis = _combine_basis(income_basis, key_basis)

    else:  # pragma: no cover - load-time validation rejects unknown ops
        raise EvaluatorError(f"unknown op {op!r} on step {step.id!r}")

    value = _apply_post(value, step.post)
    return {"value": value, "basis": basis}


def evaluate(
    domain: Domain,
    facts: dict,
    mode: str = "final",
    overlay: dict | None = None,
) -> dict[str, dict[str, Any]]:
    """Run the calculation graph, returning ``{line_id: {"value", "basis"}}`` for every compute step.

    ``facts`` maps field name -> value (a scalar, or a list for fields summed across W-2s). ``mode`` is
    ``final`` / ``provisional`` / ``what_if``; ``overlay`` (what_if) supplies hypothetical field
    values that win over both facts and defaults. Deterministic: identical inputs yield an identical
    dict on every call.
    """
    if mode not in ("final", "provisional", "what_if"):
        raise EvaluatorError(f"unknown mode {mode!r}")
    run = _Run(domain, facts, mode, overlay)
    for step in domain.compute_steps:  # ordered, acyclic — a single forward pass suffices
        run.results[step.id] = _eval_step(run, step)
    return run.results


def bottom_line(domain: Domain, results: dict[str, dict[str, Any]]) -> float | None:
    """The single signed human-facing result: a refund is positive, an amount owed is negative.

    Read from ``compute.bottom_line`` (``refund_when {gt: [refund_line, 0]}``). Returns ``None`` if
    the deciding lines are unknown — so the S4 ranking treats an unknown bottom line as no signal.
    """
    cfg = domain.compute.get("bottom_line") or {}
    refund_line = cfg.get("refund")
    owed_line = cfg.get("owed")
    refund = (results.get(refund_line) or {}).get("value") if refund_line else None
    owed = (results.get(owed_line) or {}).get("value") if owed_line else None
    if refund is not None and refund > 0:
        return float(refund)
    if owed is not None and owed > 0:
        return -float(owed)
    if refund is not None:
        return float(refund)
    if owed is not None:
        return -float(owed)
    return None
