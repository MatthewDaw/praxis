"""A DECLARED check's own command must survive the pin path.

Regression cover for a live defect: ``pin_validations`` took each entry's ``run`` verbatim from the
worker, so a check could be pinned under its own ``validation_id`` carrying a command that tested
something else entirely, be recorded as passing, and finish the ticket. Measured on a real plan,
7 of 20 pinned checks did not match their stored definition — a 3800-char UI check pinned as a
171-char "start the server and curl one route", and a lint/typecheck gate cut from 166 to 74 chars.
Every substitution was weaker than the check it displaced, and the ticket went green over a stub.

The fix keeps worker authorship for everything a check does NOT spell out (the acceptance floor,
graded/rubric checks) and makes the authored command authoritative wherever one is declared.
"""

import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
SRC = Path(__file__).resolve().parents[1] / "src"
for p in (str(SRC), str(HOOKS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import _ticket_state as ts  # noqa: E402


REAL_RUN = "bash -c 'set -u; assert byte floors; assert layer ids; assert F6 vocabulary'"
WORKER_SUBSTITUTE = "bash -c 'curl -sf http://127.0.0.1:8791/runs >/dev/null'"


def _stash(mapping):
    return {ts.M_AUTHORED_RUNS: mapping}


def test_authored_run_overrides_a_worker_substitute():
    pinned = [{"validation_id": "chk-ui", "covers": ["chk-ui"], "run": WORKER_SUBSTITUTE}]
    ts._apply_authored_runs(pinned, _stash({"chk-ui": REAL_RUN}))
    assert pinned[0]["run"] == REAL_RUN
    assert pinned[0]["run_source"] == "authored"


def test_worker_authorship_survives_where_no_run_is_declared():
    """The acceptance floor and any check that deliberately leaves the command to the builder."""
    pinned = [{"validation_id": "r1::acceptance", "covers": ["r1"], "run": "pytest tests/acc -q"}]
    ts._apply_authored_runs(pinned, _stash({"chk-ui": REAL_RUN}))
    assert pinned[0]["run"] == "pytest tests/acc -q"
    assert pinned[0]["run_source"] == "worker"


def test_graded_check_is_untouched():
    """A graded check carries a frozen rubric and no run; the override must not disturb it."""
    pinned = [{"validation_id": "g1", "covers": ["g1"], "run": "",
               "kind": "graded", "rubric": {"axes": [{"name": "x", "threshold": 0.9}]}}]
    ts._apply_authored_runs(pinned, _stash({}))
    assert pinned[0]["kind"] == "graded"
    assert pinned[0]["rubric"]["axes"][0]["name"] == "x"
    assert pinned[0]["run"] == ""


def test_an_empty_or_malformed_stash_degrades_to_worker_authorship():
    for bad in ({}, {ts.M_AUTHORED_RUNS: None}, {ts.M_AUTHORED_RUNS: "not-a-dict"},
                {ts.M_AUTHORED_RUNS: []}):
        pinned = [{"validation_id": "x", "covers": ["x"], "run": "worker cmd"}]
        ts._apply_authored_runs(pinned, bad)
        assert pinned[0]["run"] == "worker cmd"
        assert pinned[0]["run_source"] == "worker"


def test_a_blank_authored_run_does_not_blank_the_workers():
    """A check whose meta.run is empty/whitespace declares nothing — it must not erase the command."""
    for blank in ("", "   ", "\n"):
        pinned = [{"validation_id": "c", "covers": ["c"], "run": "worker cmd"}]
        ts._apply_authored_runs(pinned, _stash({"c": blank}))
        assert pinned[0]["run"] == "worker cmd"
        assert pinned[0]["run_source"] == "worker"


def test_pin_requirements_captures_declared_runs_only():
    """The stash is built from the resolved check facts, keyed by the id the pinned entry carries."""
    captured = {}

    def fake_write_build_state(cid, patch, **kw):
        captured.update(patch)
        return {"ok": True}

    real = ts._praxis.write_build_state
    ts._praxis.write_build_state = fake_write_build_state
    try:
        ts.pin_requirements("ticket-1", [
            {"id": "chk-ui", "meta": {"run": REAL_RUN}},
            {"id": "chk-graded", "meta": {"rubric": {"axes": []}}},      # no run -> not captured
            {"id": "chk-blank", "meta": {"run": "  "}},                   # blank -> not captured
        ])
    finally:
        ts._praxis.write_build_state = real

    stash = captured[ts.M_AUTHORED_RUNS]
    assert stash == {"chk-ui": REAL_RUN}
    assert captured[ts.M_PINNED_CHECKS] == []
