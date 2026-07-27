#!/usr/bin/env python3
"""Eval for plan-content fingerprint allow-list (ticket 76e3ba37760647a7ac7d7f7168ed680f).

Acceptance criteria:
  1. A claim, heartbeat and release cycle leaves the fingerprint unchanged
  2. An edit to a requirement's text or acceptance changes it
  3. Adding a NEW lifecycle meta key to a ticket also leaves it unchanged

We test the GATE'S ACTUAL _snapshot_hash by monkeypatching _praxis.facts_by.
"""
import hashlib
import json
import sys

# Import from the worktree where the fix lives
_WORKTREE = '/workspace/praxis/.claude/worktrees/wf_6bf1c062-02d-6'
sys.path.insert(0, f'{_WORKTREE}/agent_factory/hooks')
sys.path.insert(0, f'{_WORKTREE}/agent_factory/src')

import _praxis
import _ticket_state as ts
import plan_completeness_gate as gate


def make_fact(fact_id, text, acceptance, depends_on, tags, verify, defines, references, extra_meta=None):
    """Build a fact dict shaped like what _praxis.facts_by returns."""
    meta = {
        "requirement_id": fact_id,
        "acceptance": acceptance,
        "depends_on": depends_on,
        "tags": tags,
        "verify": verify,
        "defines": defines,
        "references": references,
    }
    if extra_meta:
        meta.update(extra_meta)
    return {
        "id": f"fact-{fact_id}",
        "factId": f"fact-{fact_id}",
        "text": text,
        "content": text,
        "meta": meta,
    }


BASE_FACTS = [
    make_fact("R1", "User can log in", "Login form appears", [], ["auth"], "automated", ["auth"], []),
    make_fact("R2", "User can sign up", "Registration form works", ["R1"], ["auth"], "automated", ["signup"], ["auth"]),
]

LIFECYCLE_META = {
    "build_state": "claimed",
    "pinned_checks": ["c1", "c2"],
    "claimed_by": "agent-1",
    "heartbeat_at": "2025-01-01T00:00:00Z",
    "validation_results": {"c1": "passed", "c2": "passed"},
    "complete": True,
}

FACTS_WITH_LIFECYCLE = [
    make_fact("R1", "User can log in", "Login form appears", [], ["auth"], "automated", ["auth"], [],
              extra_meta=LIFECYCLE_META),
    make_fact("R2", "User can sign up", "Registration form works", ["R1"], ["auth"], "automated", ["signup"], ["auth"]),
]

FACTS_TEXT_CHANGED = [
    make_fact("R1", "User can log in with email and SSO", "Login form appears", [], ["auth"], "automated", ["auth"], []),
    make_fact("R2", "User can sign up", "Registration form works", ["R1"], ["auth"], "automated", ["signup"], ["auth"]),
]

FACTS_ACCEPTANCE_CHANGED = [
    make_fact("R1", "User can log in", "Login form with biometric auth appears", [], ["auth"], "automated", ["auth"], []),
    make_fact("R2", "User can sign up", "Registration form works", ["R1"], ["auth"], "automated", ["signup"], ["auth"]),
]

FACTS_NEW_LIFECYCLE_KEY = [
    make_fact("R1", "User can log in", "Login form appears", [], ["auth"], "automated", ["auth"], [],
              extra_meta={"build_state": "claimed"}),
    make_fact("R2", "User can sign up", "Registration form works", ["R1"], ["auth"], "automated", ["signup"], ["auth"],
              extra_meta={"build_state": "incomplete"}),
]

# Monkey-patch _praxis.facts_by to return our controlled data
original_facts_by = _praxis.facts_by
_current_facts = BASE_FACTS

def _mock_facts_by(**kwargs):
    return list(_current_facts)

_praxis.facts_by = _mock_facts_by

# Also need to stub project_ref to return a known plan tuple
_original_project_ref = ts.project_ref
def _mock_project_ref(project):
    class Ref:
        plan = ("af-super-run", "prd-af-super-run")
    return Ref()
ts.project_ref = _mock_project_ref

errors = []

try:
    # --- Compute base hash ---
    _current_facts = BASE_FACTS
    h_base = gate._snapshot_hash("af-super-run")
    print(f"Hash (base facts):              {h_base}")

    # --- Test 1: lifecycle meta MUST NOT change the hash ---
    _current_facts = FACTS_WITH_LIFECYCLE
    h_lifecycle = gate._snapshot_hash("af-super-run")
    print(f"Hash (lifecycle meta added):    {h_lifecycle}")

    if h_base != h_lifecycle:
        errors.append(
            f"FAIL (criterion 1): fingerprint changed when lifecycle meta was added: "
            f"{h_base} -> {h_lifecycle}. Lifecycle meta keys (build_state, pinned_checks, "
            f"claimed_by, heartbeat_at, validation_results, complete) should NOT affect "
            f"the snapshot fingerprint."
        )

    # --- Test 2: text change MUST change the hash ---
    _current_facts = FACTS_TEXT_CHANGED
    h_text = gate._snapshot_hash("af-super-run")
    print(f"Hash (text changed):            {h_text}")

    if h_base == h_text:
        errors.append(
            f"FAIL (criterion 2): fingerprint did NOT change when requirement text was edited: "
            f"{h_base} == {h_text}. Text changes should affect the fingerprint."
        )

    # --- Test 3: acceptance change MUST change the hash ---
    _current_facts = FACTS_ACCEPTANCE_CHANGED
    h_accept = gate._snapshot_hash("af-super-run")
    print(f"Hash (acceptance changed):      {h_accept}")

    if h_base == h_accept:
        errors.append(
            f"FAIL (criterion 3): fingerprint did NOT change when acceptance was edited: "
            f"{h_base} == {h_accept}. Acceptance changes should affect the fingerprint."
        )

    # --- Test 4: NEW lifecycle meta key MUST NOT change the hash ---
    _current_facts = FACTS_NEW_LIFECYCLE_KEY
    h_new_key = gate._snapshot_hash("af-super-run")
    print(f"Hash (new lifecycle key only):  {h_new_key}")

    if h_base != h_new_key:
        errors.append(
            f"FAIL (criterion 4): fingerprint changed when a NEW lifecycle meta key "
            f"(build_state) was added: {h_base} -> {h_new_key}. "
            f"New lifecycle keys should NOT affect the snapshot fingerprint."
        )

finally:
    # Restore originals
    _praxis.facts_by = original_facts_by
    ts.project_ref = _original_project_ref


# --- Report ---
if errors:
    print(f"\n{'='*60}")
    print(f"EVAL RESULT: FAIL ({len(errors)} error(s))")
    for e in errors:
        print(f"  {e}")
    print(f"{'='*60}")
    sys.exit(1)
else:
    print(f"\n{'='*60}")
    print("EVAL RESULT: PASS — fingerprint uses content-only allow-list")
    print(f"{'='*60}")
    sys.exit(0)
