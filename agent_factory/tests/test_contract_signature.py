"""Locks U3: the PURE signed-contract payload/validation helpers (``contract_signature``).

No I/O — every function is total over dicts. ``is_signed`` / ``actions_recorded`` are the two halves
of the hard bless predicate (KTD3): signed (kind + signer) AND real evaluator actions recorded — a
bare padded count is neither. ``below_floor`` is the SOFT flag, never a gate.
"""

from agent_factory import contract_signature as cs


# --------------------------------------------------------------------------- build

def test_build_signed_payload_shape():
    p = cs.build_signed_payload(12, {"cut": 2, "merged": 1, "added": 3}, "evaluator")
    assert p["kind"] == cs.CONTRACT_KIND
    assert p["n_assertions"] == 12
    assert p["actions"] == {"cut": 2, "merged": 1, "added": 3}
    assert p["signer"] == "evaluator"


def test_build_signed_payload_coerces_and_defaults_missing_action_buckets():
    p = cs.build_signed_payload("7", {"cut": "1"}, "  evaluator  ")
    assert p["n_assertions"] == 7
    assert p["actions"] == {"cut": 1, "merged": 0, "added": 0}
    assert p["signer"] == "evaluator"


# --------------------------------------------------------------------------- is_signed

def test_is_signed_true_on_wellformed_payload():
    assert cs.is_signed(cs.build_signed_payload(12, {"cut": 1}, "evaluator")) is True


def test_is_signed_true_on_readback_fact_with_meta_episode():
    fact = {"id": "e1", "meta": {"episode": cs.build_signed_payload(12, {"added": 1}, "evaluator")}}
    assert cs.is_signed(fact) is True
    assert cs.actions_recorded(fact) is True


def test_is_signed_false_on_bare_count():
    # A padded count with no kind/signer is NOT a signed contract.
    assert cs.is_signed({"n_assertions": 99}) is False


def test_is_signed_false_without_signer():
    assert cs.is_signed({"kind": cs.CONTRACT_KIND, "n_assertions": 12, "actions": {"cut": 1}}) is False


def test_is_signed_false_on_malformed():
    for bad in (None, "nope", 5, [], {}):
        assert cs.is_signed(bad) is False


# --------------------------------------------------------------------------- actions_recorded

def test_actions_recorded_true_when_any_action_nonzero():
    assert cs.actions_recorded(cs.build_signed_payload(12, {"cut": 0, "merged": 0, "added": 1},
                                                       "e")) is True


def test_actions_recorded_false_on_all_zero_actions():
    # signed over an unchanged draft (padded) -> no real evaluator action -> fails the anti-Goodhart gate.
    assert cs.actions_recorded(cs.build_signed_payload(50, {"cut": 0, "merged": 0, "added": 0},
                                                       "e")) is False


def test_actions_recorded_false_when_actions_missing_or_malformed():
    assert cs.actions_recorded({"kind": cs.CONTRACT_KIND, "signer": "e"}) is False
    assert cs.actions_recorded({"kind": cs.CONTRACT_KIND, "signer": "e", "actions": "lots"}) is False


# --------------------------------------------------------------------------- below_floor

def test_below_floor_boundary():
    assert cs.below_floor(9, floor=10) is True
    assert cs.below_floor(10, floor=10) is False
    assert cs.below_floor(11, floor=10) is False


def test_below_floor_default_and_malformed():
    assert cs.below_floor(3) is True                     # default floor is 10
    assert cs.below_floor(None) is True                  # malformed -> flag for attention
