"""Reserved space names — the single typed source of truth.

Under the canonical layout every project is ONE org-shared space named exactly the project, holding
per-role SNAPSHOTS (``prd-<project>`` for the plan/ticket state, ``building-validation`` /
``planning-validation`` for the per-scope checks). The retired *standalone* layout modelled those
roles as top-level SPACES; this module makes that layout unrepresentable by reserving those names
(plus the eval space and any ``<x>-plan`` slug) so they can never again be created as a space.

Pure name logic — NO DB imports — so both ``app.py`` and ``spaces_store`` can import it without a
cycle. CONTENT separation (a ``category="check"`` fact may not live in a ``prd-*`` plan snapshot, and
a ``*-validation`` snapshot admits only its scope's checks) is a WRITE-TIME invariant enforced at the
store layer (``postgres_vector_graph`` ``SnapshotKindError``); this module only reserves NAMES.
"""

from __future__ import annotations

# The eval cache lives in this org-scoped reserved space (never a user-created space).
RESERVED_EVAL_SPACE = "__evals__"

# The retired standalone layout: once top-level SPACES, now per-scope SNAPSHOT roles inside a single
# project space, so they may never again be created as standalone spaces. ``build-plan`` is also
# covered by the ``-plan`` suffix rule below but is listed explicitly for clarity.
_RETIRED_STANDALONE_SPACES = frozenset(
    {"coding-validation", "building-validation", "planning-validation", "build-plan"}
)


def is_reserved_space_id(space_id: str) -> bool:
    """True if ``space_id`` may NOT be created as an org space.

    Reserved: the eval space, any retired standalone-layout id, or any ``<x>-plan`` slug (a plan
    snapshot role inside a project space, never a space of its own).
    """
    return (
        space_id == RESERVED_EVAL_SPACE
        or space_id in _RETIRED_STANDALONE_SPACES
        or space_id.endswith("-plan")
    )
