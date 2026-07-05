"""U3 — the field validator (S6, the typed boundary).

Nothing reaches a recorded outcome without passing through here. A gathered value is validated
against its ``fields.yaml`` schema (enum / number / integer / boolean / string with min/max/
max_length) and against the pack's cross-field invariants (e.g. withholding ≤ wages). Invalid input
is **rejected with a structured error, never coerced** — mirroring the harness Pydantic boundary
(``app/schemas.py``).

Errors are structured (`field` + `reason`) so the loop can name the rule that rejected the value
instead of silently dropping it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .domain import Domain

# Cross-field invariant predicates (closed set; mirrors policy.yaml guardrails.invariants).
_PREDICATES = {
    "lte": lambda a, b: a <= b,
    "lt": lambda a, b: a < b,
    "gte": lambda a, b: a >= b,
    "gt": lambda a, b: a > b,
    "eq": lambda a, b: a == b,
}


@dataclass(frozen=True)
class Result:
    """Validation outcome. ``ok`` True means the (possibly coerced-by-type) value is in ``value``;
    False means rejected, with a structured ``field``/``reason``."""

    ok: bool
    field: str = ""
    reason: str = ""
    value: Any = None

    def __bool__(self) -> bool:  # so `if validate_field(...):` reads naturally
        return self.ok


def _error(field: str, reason: str) -> Result:
    return Result(ok=False, field=field, reason=reason)


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):  # bool is an int subclass — never a number here
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def validate_field(domain: Domain, name: str, value: Any) -> Result:
    """Validate ``value`` against field ``name``'s ``fields.yaml`` schema.

    Returns an ``ok`` :class:`Result` carrying the type-normalized value, or a structured error
    naming the field. An unknown field name is itself a rejection (the boundary is closed).
    """
    schema = domain.field_schemas.get(name)
    if schema is None:
        return _error(name, f"unknown field {name!r}")
    ftype = schema.get("type")

    if ftype == "enum":
        allowed = schema.get("values") or []
        if value not in allowed:
            return _error(name, f"{value!r} is not one of {allowed}")
        return Result(ok=True, field=name, value=value)

    if ftype in ("number", "integer"):
        num = _as_number(value)
        if num is None:
            return _error(name, f"{value!r} is not a {ftype}")
        if ftype == "integer" and float(num) != int(num):
            return _error(name, f"{value!r} is not a whole number")
        lo, hi = schema.get("min"), schema.get("max")
        if lo is not None and num < lo:
            return _error(name, f"{num} is below minimum {lo}")
        if hi is not None and num > hi:
            return _error(name, f"{num} is above maximum {hi}")
        return Result(ok=True, field=name, value=int(num) if ftype == "integer" else num)

    if ftype == "boolean":
        if isinstance(value, bool):
            return Result(ok=True, field=name, value=value)
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no"):
            return Result(ok=True, field=name, value=value.strip().lower() in ("true", "yes"))
        return _error(name, f"{value!r} is not a boolean")

    if ftype == "string":
        if not isinstance(value, str):
            return _error(name, f"{value!r} is not a string")
        max_len = schema.get("max_length")
        if max_len is not None and len(value) > max_len:
            return _error(name, f"string exceeds max_length {max_len}")
        return Result(ok=True, field=name, value=value)

    return _error(name, f"unsupported field type {ftype!r}")


def validate_cross_field(domain: Domain, facts: dict) -> Result:
    """Check the pack's cross-field invariants over the gathered ``facts``.

    Each invariant is ``{op, left, right, error}`` (a closed predicate set). An invariant whose
    operands are not both present is skipped (you cannot violate a rule you have no data for).
    Returns the first violation as a structured error, else an ok :class:`Result`.
    """
    for inv in domain.cross_field:
        op = inv.get("op")
        left_name, right_name = inv.get("left"), inv.get("right")
        if op not in _PREDICATES:
            continue
        if left_name not in facts or right_name not in facts:
            continue
        left, right = _as_number(facts[left_name]), _as_number(facts[right_name])
        if left is None or right is None:
            continue
        if not _PREDICATES[op](left, right):
            reason = inv.get("error") or f"{left_name} {op} {right_name} violated"
            # name the LEFT operand as the suspect field (for an `lte`/`lt` it is the over-large
            # value; callers re-ask / reject that field rather than coercing it into the deliverable).
            return _error(str(left_name), reason)
    return Result(ok=True)
