"""Box-service model-backend management: read/write the active backend choice (R88).

The active backend (``sonnet`` | ``deepseek``) is persisted as a machine-wide file
setting, read at session-launch time. Switching it via MCP replaces the active
choice without interrupting already-running sessions — only sessions launched
after the switch read the new value.

**Exclusivity guarantee** (mirroring ``af-backend``): when one backend is active
and its credential is set in the box service's own environment, the OTHER
backend's credential must NOT be set. The session-launch path
(:func:`build_session_env.build_session_environment` / :mod:`dispatch_launch`)
reads the active choice from this module and only exposes the selected
credential to the launched session.

Pure decision logic — no subprocess, no I/O beyond the file read/write.
"""

from __future__ import annotations

import os

#: The valid backend identifiers.
VALID_BACKENDS: frozenset[str] = frozenset({"sonnet", "deepseek"})

#: Default path for the machine-wide backend-choice file. Override by setting
#: ``PRAXIS_BACKEND_FILE`` in the box service's environment before startup.
DEFAULT_BACKEND_FILE: str = os.path.join(os.path.expanduser("~"), ".praxis", "backend")

#: The credential env var each backend requires — a backend is **only** valid when
#: its credential IS set in the box service's own process environment (its absence
#: means the box was not provisioned for that backend). The OTHER backend's
#: credential must be absent — the exclusivity guarantee.
BACKEND_CREDENTIAL_VAR: dict[str, str] = {
    "sonnet": "ANTHROPIC_API_KEY",
    "deepseek": "OPENROUTER_API_KEY",
}


def _backend_file() -> str:
    """The file path holding the active backend. Overridable via env var for testing
    and for boxes that need a non-default location."""
    return os.environ.get("PRAXIS_BACKEND_FILE", DEFAULT_BACKEND_FILE)


def read_active_backend() -> str:
    """Return the active backend identifier (``"sonnet"`` or ``"deepseek"``).

    Raises :class:`FileNotFoundError` when no backend has been set yet, and
    :class:`ValueError` when the persisted value is not one of
    :data:`VALID_BACKENDS`.
    """
    path = _backend_file()
    try:
        with open(path) as fh:
            choice = fh.read().strip()
    except FileNotFoundError:
        raise FileNotFoundError(
            f"no active backend set — file {path!r} does not exist. "
            f"Write one of {sorted(VALID_BACKENDS)!r} to it, or call "
            f"praxis_switch_backend."
        )
    choice = choice.strip().casefold()
    if choice not in VALID_BACKENDS:
        raise ValueError(
            f"unknown backend {choice!r} persisted in {path!r}; "
            f"valid choices: {sorted(VALID_BACKENDS)!r}"
        )
    return choice


def write_active_backend(choice: str) -> None:
    """Persist ``choice`` as the active backend.

    Normalizes the value (``strip().casefold()``), validates it against
    :data:`VALID_BACKENDS`, and writes it atomically to the backend file so a
    concurrent reader never sees a partial write.

    Raises :class:`ValueError` when ``choice`` is not one of :data:`VALID_BACKENDS`.
    """
    choice = choice.strip().casefold()
    if choice not in VALID_BACKENDS:
        raise ValueError(
            f"invalid backend {choice!r}; must be one of {sorted(VALID_BACKENDS)!r}"
        )
    path = _backend_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Atomic write: write to a temp file then rename, so a concurrent reader
    # never sees a truncated value.
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(choice)
    os.replace(tmp, path)


def backend_session_credential(*, default_backend: str | None = None) -> dict[str, str]:
    """Return ``{CREDENTIAL_VAR: value}`` for the active backend, or ``{}``.

    Reads the active backend from the persisted file and, IF the box service's
    own process environment carries a non-empty value for that backend's
    credential var, returns it as a single-entry dict suitable for merging into
    a launched session's environment. Returns an empty dict when:

    - The backend file doesn't exist yet (never provisioned).
    - The persisted value is unknown (not in :data:`VALID_BACKENDS`).
    - The credential var is absent or empty in the box service's own env (the
      box was not provisioned for that backend — the exclusivity guarantee's
      other half, because the non-selected credential MUST be absent).

    ``default_backend`` is an override for testing (the active-backend file path
    is already overridable via ``PRAXIS_BACKEND_FILE``, but a caller that wants to
    bypass the file entirely may pass the backend name here).

    The caller is responsible for merging the returned dict into the session
    environment — this function never mutates ``os.environ``.
    """
    try:
        choice = default_backend or read_active_backend()
    except (FileNotFoundError, ValueError):
        return {}
    if choice not in VALID_BACKENDS:
        return {}
    var = BACKEND_CREDENTIAL_VAR.get(choice, "")
    if not var:
        return {}
    value = os.environ.get(var, "")
    if not value:
        return {}
    return {var: value}
