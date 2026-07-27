"""Build-session environment isolation (R37): the push credential must not be
reachable from a build session.

File-permission separation through a distinct operating-system user is necessary but not
sufficient — a credential the box service resolves (``github_token.resolve_github_token``)
lives only in the box service's own process memory (see ``box_service_push_auth``); this
module closes the OTHER half of the gap, that a launched build session's process
environment must not itself carry a copy of the service token / secret name that names the
credential, nor any ambient cloud-credential variable the box service's own process might
carry.

The session launches with an ALLOWLIST-scrubbed environment (a positive list of what a
build session needs — :data:`ALLOWED_ENV_VARS`), not a blocklist: a secret variable the box
service's own environment picks up in the future is excluded by default rather than
requiring this module to be updated to keep excluding it. It also gets its own ``HOME``,
distinct from the box service's, so no per-user credential file (``~/.aws/credentials``,
``~/.netrc``, a cached CLI token, ...) the box service's home directory holds is inherited
by the session either.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

#: What a launched build session needs to run and to talk back to Praxis (see
#: ``docs/factory-state-contract.md``) — nothing else survives the scrub. In particular
#: this excludes every GitHub push-credential variable (``GITHUB_TOKEN*``) and every AWS/cloud
#: credential variable (``AWS_*``, ``GOOGLE_APPLICATION_CREDENTIALS``, ...) the box service's
#: own process may carry.
ALLOWED_ENV_VARS: frozenset[str] = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "TERM",
        "SHELL",
        "USER",
        "TZ",
        "PRAXIS_API_KEY",
        "PRAXIS_ORG",
        "PRAXIS_API_BASE_URL",
        "FACTORY_PROJECT",
    }
)


def build_session_environment(base_env: Mapping[str, str], *, home_dir: str) -> dict[str, str]:
    """The environment a launched build session runs under.

    Every variable in ``base_env`` not in :data:`ALLOWED_ENV_VARS` is dropped — in
    particular any GitHub push-credential variable and any AWS/cloud-credential variable
    the box service's own process carries — and ``HOME`` is pinned to ``home_dir`` rather
    than inherited, so the session never reads a credential file cached under the box
    service's own home directory.
    """
    scrubbed = {key: value for key, value in base_env.items() if key in ALLOWED_ENV_VARS}
    scrubbed["HOME"] = home_dir
    return scrubbed


def default_job_home(worktree_path: str) -> str:
    """The distinct home directory a job's build session gets: nested under its own
    worktree rather than the box service's ``$HOME`` (R37), so it is guaranteed to differ
    from the box service's home directory for every job."""
    return os.path.join(worktree_path, ".job-home")
