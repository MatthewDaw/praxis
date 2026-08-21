"""Compatibility facade for :mod:`knowledge.ml_registry.cli.manifests`."""

import sys

from knowledge.ml_registry.cli import manifests as _manifests
from knowledge.ml_registry.cli.manifests import *  # noqa: F403 -- former public surface


def __getattr__(name: str):
    return getattr(_manifests, name)


if __name__ == "__main__":
    sys.exit(main())  # noqa: F405 -- imported compatibility entry point
