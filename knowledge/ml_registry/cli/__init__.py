"""Stable facade for the split ML-registry command groups."""

from . import registry as _registry
from .registry import (
    _json_arg as _json_arg,
    _load_mutate_save as _load_mutate_save,
    _lock_timeout_seconds as _lock_timeout_seconds,
    load_ledger_rows as load_ledger_rows,
    main as main,
)


def __getattr__(name: str):
    """Preserve private module attributes imported from the former single-file CLI."""
    return getattr(_registry, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_registry)))
