#!/usr/bin/env python3
"""``af whoami`` — one line that says who you are, where, and in which org.

The multi-tenancy preflight: prints ``{backend, resolved_org, principal, auth_mode,
key_org_if_key}`` for the current environment by asking the server ``GET /whoami`` with
the SAME headers a real hook request sends. On a mismatch it emits a crisp diagnosis
("key scoped to org 'sotos' but PRAXIS_ORG='bestie'") and exits non-zero, so it doubles
as a fail-fast gate before an af-build run picks the wrong backend/org.

    python -m agent_factory.tools.whoami

Runs the stdlib hook client (``hooks/_praxis``) — the byte-for-byte auth path af-build's Stop
gate uses — so what this prints IS what the gate will see.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _praxis  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    who = _praxis.whoami()
    print(who.line())
    return 0 if who.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
