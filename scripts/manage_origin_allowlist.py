#!/usr/bin/env python3
"""Operator-only CLI to add or remove an origin from the dispatch origin
allowlist store (R54).

This script is the SINGLE documented path to mutate the allowlist. It is a
standalone entry point: nothing under ``knowledge/serve`` imports it, and it
imports nothing from ``dispatch.py`` or any MCP tool — the read side
(``origin_allowlist.load_allowlist``) and the write side (here) never share a
caller, so no MCP tool, dispatching agent, or box-side session can reach a
write through code that also does dispatch/build work. Run it by hand, as the
operator, directly against the store file:

    python scripts/manage_origin_allowlist.py add git@github.com:acme/widgets.git \\
        --path /path/to/allowlist.json
    python scripts/manage_origin_allowlist.py remove git@github.com:acme/widgets.git \\
        --path /path/to/allowlist.json

The store is a JSON array of origin URL strings, matching what
``origin_allowlist.load_allowlist`` reads. A missing store is treated as
empty *only* by this operator tool, on ``add`` (so the very first
registration can create the file) — every other reader (``load_allowlist``,
and therefore dispatch) still fails closed on a missing file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_PATH = "origin_allowlist.json"


def _read_origins(path: Path) -> list[str]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise ValueError(f"{path} must contain a JSON array of origin URL strings")
    return data


def _write_origins(path: Path, origins: list[str]) -> None:
    path.write_text(json.dumps(sorted(set(origins)), indent=2) + "\n", encoding="utf-8")


def add_origin(path: Path, origin: str) -> list[str]:
    """Add ``origin`` to the store at ``path`` (idempotent) and return the
    resulting list."""
    origins = _read_origins(path)
    if origin not in origins:
        origins.append(origin)
    _write_origins(path, origins)
    return sorted(set(origins))


def remove_origin(path: Path, origin: str) -> list[str]:
    """Remove ``origin`` from the store at ``path`` (idempotent — removing an
    absent origin is not an error) and return the resulting list."""
    origins = [o for o in _read_origins(path) if o != origin]
    _write_origins(path, origins)
    return sorted(set(origins))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=DEFAULT_PATH, help="path to the allowlist JSON store")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="add an origin to the allowlist")
    p_add.add_argument("origin")

    p_remove = sub.add_parser("remove", help="remove an origin from the allowlist")
    p_remove.add_argument("origin")

    args = parser.parse_args(argv)
    path = Path(args.path)

    if args.command == "add":
        origins = add_origin(path, args.origin)
    else:
        origins = remove_origin(path, args.origin)

    print(json.dumps(origins, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
