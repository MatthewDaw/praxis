"""Canonical portfolio operator command line.

The implementation remains in :mod:`knowledge.ml_registry.operator_cli` during
the semantic cutover so existing Python imports keep working. This module is
the sole public executable namespace; compatibility modules only forward here.
"""

from __future__ import annotations

import sys

from knowledge.ml_registry.operator_cli import (
    OperatorConfigError,
    OperatorRuntime,
    _parser,  # noqa: F401 -- compatibility for documented Python imports
    main,
)

__all__ = ["OperatorConfigError", "OperatorRuntime", "main"]


if __name__ == "__main__":
    sys.exit(main())
