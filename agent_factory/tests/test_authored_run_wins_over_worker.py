"""A DECLARED check's own command is the only thing that can satisfy it.

Regression cover for a defect found live. The completion gate accepted any pinned entry that named
a check in its ``covers``, regardless of what command that entry actually ran. Two ways through it,
both observed on a real plan:

1. **Alias indirection.** The worker pins under an id it invents (``v-static-hygiene``) declaring
   ``covers: [<real check fact id>]`` and carrying a command it wrote itself. A 166-char lint gate
   was pinned as 74 chars this way; a 3800-char UI check as 171.
2. **The empty append.** ``record_validation_pass`` appends ``{"run": "", "passed": True}`` for any
   id not already pinned, so recording a pass against a real check id fabricates a green entry that
   ran nothing at all. A bucket-creation ticket finished green having created no bucket, carrying
   six such entries.

The authored map is read LIVE from ``building-validation`` rather than copied onto the ticket: an
earlier attempt stashed it in ticket meta, which silently did nothing because ``write_build_state``
only accepts the server's registered ``BUILD_STATE_META_KEYS`` and drops anything else in transit.
"""

import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
SRC = Path(__file__).resolve().parents[1] / "src"
for p in (str(SRC), str(HOOKS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import _ticket_state as ts  # noqa: E402

REAL = "bash -c 'assert byte floors; assert layer ids; assert F6 vocabulary'"
WEAK = "bash -c 'curl -sf http://127.0.0.1:8791/runs >/dev/null'"
CHECK = "4c31ea8510f34b4ea7ff0b1e1f487c12"


def test_alias_entry_gets_the_authored_command_not_the_workers():
    """The worker's own id must not shield a substituted command."""
    pinned = [{"validation_id": "v-ui-serves", "covers": [CHECK], "run": WEAK}]
    ts._apply_authored_runs(pinned, {CHECK: REAL})
    assert pinned[0]["run"] == REAL
    assert pinned[0]["run_source"] == "authored"


def test_worker_authorship_survives_where_no_command_is_declared():
    pinned = [{"validation_id": "v-acc", "covers": ["r1::acceptance"], "run": "pytest tests/acc -q"}]
    ts._apply_authored_runs(pinned, {CHECK: REAL})
    assert pinned[0]["run"] == "pytest tests/acc -q"
    assert pinned[0]["run_source"] == "worker"


def test_one_entry_covering_two_declared_checks_is_flagged_ambiguous():
    """It cannot honour both commands, so it is left alone and the gate refuses it."""
    pinned = [{"validation_id": "v-both", "covers": [CHECK, "other"], "run": WEAK}]
    ts._apply_authored_runs(pinned, {CHECK: REAL, "other": "echo hi"})
    assert pinned[0]["run_source"] == "ambiguous"
    assert pinned[0]["run"] == WEAK


def test_graded_and_malformed_maps_degrade_to_worker():
    for bad in ({}, None, "not-a-dict", []):
        pinned = [{"validation_id": "g1", "covers": ["g1"], "run": "", "kind": "graded"}]
        ts._apply_authored_runs(pinned, bad)
        assert pinned[0]["run_source"] == "worker"
        assert pinned[0]["kind"] == "graded"


def _meta(required, pinned):
    return {ts.M_REQUIRED_VALIDATIONS: required, ts.M_PINNED_CHECKS: pinned}


def _gate(monkey_meta, declared):
    """Drive all_validations_passed with stubbed meta + declared-run lookup."""
    real_meta, real_decl = ts._meta, ts._declared_runs
    ts._meta = lambda *a, **k: monkey_meta
    ts._declared_runs = lambda *a, **k: declared
    try:
        return ts.all_validations_passed("ticket-1")
    finally:
        ts._meta, ts._declared_runs = real_meta, real_decl


def test_gate_rejects_an_empty_appended_entry_for_a_declared_check():
    """record_validation_pass's append path must not finish a ticket that ran nothing."""
    meta = _meta([CHECK], [{"validation_id": CHECK, "covers": [], "run": "", "passed": True}])
    assert _gate(meta, {CHECK: REAL}) is False


def test_gate_rejects_an_alias_entry_running_a_different_command():
    meta = _meta([CHECK], [{"validation_id": "v-x", "covers": [CHECK], "run": WEAK, "passed": True}])
    assert _gate(meta, {CHECK: REAL}) is False


def test_gate_accepts_the_authored_command():
    meta = _meta([CHECK], [{"validation_id": "v-x", "covers": [CHECK], "run": REAL, "passed": True}])
    assert _gate(meta, {CHECK: REAL}) is True


def test_gate_unaffected_where_no_command_is_declared():
    """An acceptance floor has no authored run; the gate must not start demanding one."""
    fid = "r1::acceptance"
    meta = _meta([fid], [{"validation_id": "v-a", "covers": [fid], "run": "pytest -q", "passed": True}])
    assert _gate(meta, {}) is True


def test_a_worktree_cd_prefix_is_the_same_gate():
    """Every ticket builds in its own worktree; prefixing the authored command to run it there is
    honest, and rejecting it would fail every ticket. Observed live on R29."""
    wt = "cd /workspace/farming_analysis/.claude/worktrees/agent-a224da98e3ea29e88 && "
    meta = _meta([CHECK], [{"validation_id": "v-x", "covers": [CHECK],
                            "run": wt + REAL, "passed": True}])
    assert _gate(meta, {CHECK: REAL}) is True


def test_only_the_leading_prefix_is_forgiven():
    """A command rewritten anywhere else is still a different gate."""
    wt = "cd /tmp/wt && "
    meta = _meta([CHECK], [{"validation_id": "v-x", "covers": [CHECK],
                            "run": wt + WEAK, "passed": True}])
    assert _gate(meta, {CHECK: REAL}) is False
    trailing = _meta([CHECK], [{"validation_id": "v-y", "covers": [CHECK],
                                "run": REAL + " || true", "passed": True}])
    assert _gate(trailing, {CHECK: REAL}) is False
