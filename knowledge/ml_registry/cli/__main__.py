"""Compatibility entry point for ``python -m knowledge.ml_registry.cli``."""

import sys

from .registry import main


if __name__ == "__main__":
    sys.exit(main())
