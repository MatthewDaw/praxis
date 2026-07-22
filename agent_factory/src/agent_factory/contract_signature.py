"""Pure payload/validation helpers for the signed-contract episode (idea 1 — adversarial contract
negotiation).

Intake hardening becomes a planner DRAFT plus a SEPARATE evaluator role that adversarially
rewrites/cuts/adds testable assertions and **signs** the result — recorded as a ``contract-signed``
decision-log episode carrying the assertion COUNT and the evaluator's cut/merge/add ACTIONS.

This module is PURE over dicts — no Praxis client, no I/O. That is deliberate: ``src/agent_factory``
has no Praxis client and must not grow one (feasibility finding). The actual episode read/write lives
in ``hooks/_praxis.py`` (``record_episode`` / ``get_episodes``) and the skill's ``praxis_record_episode``
MCP call; this module only BUILDS the payload and VALIDATES a read-back episode.

Anti-Goodhart (KTD3): the HARD bless predicate is "signed AND actions recorded", NOT "count >= N". A
raw ``n_assertions >= floor`` count is a target an evaluator clears by padding, so the count is
recorded and merely FLAGS for attention below the floor (``below_floor``) — it is never the gate.
"""

from __future__ import annotations

from typing import Any

# The episode ``kind`` that marks a signed contract. Part of the module's public contract — the gate
# (``R-CONTRACT-SIGNED``) and the skill's MCP write agree on this string.
CONTRACT_KIND = "contract-signed"

# The default "enough concrete assertions" floor. A requirement below it is FLAGGED for the evaluator
# (soft signal), never hard-rejected — reconciling the "flags, not rejects" intent.
DEFAULT_ASSERTION_FLOOR = 10

# The evaluator action buckets recorded on a signed contract.
_ACTION_KEYS = ("cut", "merged", "added")


def build_signed_payload(n_assertions: int, actions: dict[str, int], signer: str) -> dict[str, Any]:
    """Build the ``meta.episode`` payload for a signed contract.

    ``n_assertions`` is the testable-assertion count (informational — recorded, never the gate);
    ``actions`` is the evaluator's cut/merged/added counts; ``signer`` names the evaluator role that
    signed (the planner never signs its own contract). The returned dict is what
    ``_praxis.record_episode(episode=...)`` stores and what :func:`is_signed` / :func:`actions_recorded`
    validate on read-back.
    """
    acts = {k: int((actions or {}).get(k, 0) or 0) for k in _ACTION_KEYS}
    return {
        "kind": CONTRACT_KIND,
        "n_assertions": int(n_assertions or 0),
        "actions": acts,
        "signer": str(signer or "").strip(),
    }


def _payload(episode: Any) -> dict:
    """Extract the contract payload from either a raw payload dict OR a read-back fact
    (``meta.episode``), so the validators accept whatever the caller has in hand."""
    if not isinstance(episode, dict):
        return {}
    meta = episode.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("episode"), dict):
        return meta["episode"]
    return episode


def is_signed(episode: Any) -> bool:
    """True iff ``episode`` is a well-formed signed contract: the ``contract-signed`` kind AND a
    non-empty signer. A bare count with no signer/kind (a padded stand-in) is NOT signed."""
    ep = _payload(episode)
    if ep.get("kind") != CONTRACT_KIND:
        return False
    return bool(str(ep.get("signer") or "").strip())


def actions_recorded(episode: Any) -> bool:
    """True iff the signed contract records >=1 REAL evaluator action (cut/merged/added). This is the
    anti-Goodhart gate: a contract signed over an unchanged draft (all-zero actions) does NOT pass —
    the evaluator must have actually falsified/cut/merged/tightened something."""
    ep = _payload(episode)
    actions = ep.get("actions")
    if not isinstance(actions, dict):
        return False
    total = 0
    for k in _ACTION_KEYS:
        try:
            total += int(actions.get(k, 0) or 0)
        except (TypeError, ValueError):
            return False
    return total > 0


def below_floor(n_assertions: int, floor: int = DEFAULT_ASSERTION_FLOOR) -> bool:
    """True iff the assertion count is under the floor — a SOFT flag for evaluator attention, never a
    hard reject (KTD3)."""
    try:
        return int(n_assertions) < int(floor)
    except (TypeError, ValueError):
        return True
