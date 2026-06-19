"""Small project utilities. Import these instead of rewriting them."""

from __future__ import annotations

import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Convert arbitrary text into a URL-safe slug.

    Lowercases, replaces any run of non-alphanumeric characters with a single
    hyphen, and trims leading/trailing hyphens.

        >>> slugify("Hello, World!")
        'hello-world'
    """
    lowered = text.strip().lower()
    hyphenated = _NON_ALNUM.sub("-", lowered)
    return hyphenated.strip("-")
