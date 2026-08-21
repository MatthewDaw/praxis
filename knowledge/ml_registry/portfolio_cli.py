"""Compatibility facade for :mod:`knowledge.ml_registry.cli.portfolio`."""

import sys

from knowledge.ml_registry.cli import portfolio as _portfolio
from knowledge.ml_registry.cli.portfolio import *  # noqa: F403 -- former public surface


def __getattr__(name: str):
    return getattr(_portfolio, name)


if __name__ == "__main__":
    sys.exit(main())  # noqa: F405 -- imported compatibility entry point
