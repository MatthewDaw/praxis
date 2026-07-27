"""Origin allowlist governance (R54): the origin allowlist dispatch validates
against (see ``dispatch.validate_origin_allowlist``, R8) is provisioned
out-of-band by the operator only.

This module is deliberately **read-only**: it exposes exactly one function,
``load_allowlist``, and no add/remove/write capability at all. Any MCP tool,
dispatching agent, or box-side session imports this module (directly or
transitively) and can therefore never mutate the allowlist store through it —
there is nothing here to call. The single documented way to add or remove an
origin is the operator CLI, ``scripts/manage_origin_allowlist.py``, which is a
standalone script a human runs directly against the store file; it is never
imported by ``dispatch.py``, any MCP tool, or any box-service module, so the
write path and the read path never share a caller.

Fail-closed on an unreadable store: a missing file, a permission error, or a
store that fails to parse as a JSON array of origin URLs all raise
``AllowlistUnreadableError`` rather than being treated as an empty allowlist.
An empty allowlist and an unreadable one both ultimately refuse every origin
once ``dispatch.validate_origin_allowlist`` runs, but they are not the same
condition: an unreadable store is a operator/infra defect that must be raised
loudly, not quietly coerced into "nothing registered yet".
"""

from __future__ import annotations

import json
from pathlib import Path


class AllowlistUnreadableError(RuntimeError):
    """Raised when the origin allowlist store cannot be read: missing,
    permission-denied, not valid JSON, or not a JSON array of origin URLs.
    Dispatch must refuse rather than proceed on this (R54) — never silently
    fall back to an empty/default allowlist."""


def load_allowlist(path: str | Path) -> frozenset[str]:
    """Read-only load of the operator-provisioned origin allowlist store at
    ``path``: a JSON array of origin URL strings.

    Raises :class:`AllowlistUnreadableError` (never returns a fallback) when
    the store is missing, unreadable, malformed JSON, or not a JSON array —
    so a caller that cannot read the true allowlist refuses rather than
    treating that as "no origins registered" would (which happens to also
    refuse everything, but for a different, silently-swallowed reason).
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise AllowlistUnreadableError(
            f"origin allowlist unreadable at {path!s}: {exc}"
        ) from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AllowlistUnreadableError(
            f"origin allowlist at {path!s} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise AllowlistUnreadableError(
            f"origin allowlist at {path!s} must be a JSON array of origin URL strings"
        )

    return frozenset(data)
