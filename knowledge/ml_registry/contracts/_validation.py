from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any


class ContractError(ValueError):
    """A serialized campaign contract is malformed or unsupported."""


def exact_keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ContractError(f"unknown {label} fields: {', '.join(unknown)}")


def text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{label} must be a non-empty string")
    return value.strip()


def number(value: object, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ContractError(f"{label} must be a finite number")
    result = float(value)
    if minimum is not None and result < minimum:
        raise ContractError(f"{label} must be at least {minimum}")
    return result


def integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError(f"{label} must be an integer at least {minimum}")
    return value
