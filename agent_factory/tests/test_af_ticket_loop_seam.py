"""The SEAM tests: does `af-ticket-loop.sh` — the running system — actually invoke the
failure-learning-loop capabilities, or do they merely exist?

Every other test in this suite imports a module and calls it directly. That is exactly how an
18-ticket subsystem shipped with 1207 green tests and ZERO production callers: the loop driver kept
doing everything the old way and nothing asserted otherwise. So these tests do not import the
loop's logic — there is no such module, the logic lives inside `python - <<'PYEOF'` heredocs in a
shell script. They READ THE SHIPPED SCRIPT, extract the exact bytes it will send to the interpreter,
and EXECUTE them against instrumented modules.

That makes the tests sensitive to the thing that actually matters: if someone reverts the driver to
`_praxis.regress_requirements`, or swaps `resolve_or_defeat` back to `ts.resolve_finding`, these
fail — even though every unit test of those modules would still pass.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

# IMPORT ORDER IS LOAD-BEARING, and getting it backwards is why this file passed in a full-suite run
# and FAILED in isolation (17 tests, all "Praxis GET /facts/by -> HTTP 404" against the real
# backend). `hooks/_praxis.py` is reachable as both `_praxis` and `hooks._praxis`; the canonical
# single object is minted by `hooks._ticket_state._canonical_module` and registered under both names,
# but only once something imports the seam through `agent_factory._hooks`. Import the bare names
# FIRST and Python executes the file a second time, so the module the test monkeypatches and the
# module the driver's exec'd heredoc gets from `sys.modules` are two different objects — the patch
# silently does nothing and the block talks to production. Importing `agent_factory` first
# canonicalizes, and the bare imports below then resolve to that same object.
from agent_factory import failure_taxonomy, ingestion_api, resolution, widening  # noqa: E402

# The BARE names, deliberately — the driver's heredocs do `import _praxis, _ticket_state as ts`.
import _praxis  # noqa: E402
import _ticket_state as ts  # noqa: E402


def test_the_test_module_patches_the_same_objects_the_driver_will_import():
    """The guard on the paragraph above: if these ever fork again, every monkeypatch in this file
    becomes a no-op and the suite goes green while talking to the real backend."""
    assert sys.modules["_praxis"] is _praxis
    assert sys.modules["_ticket_state"] is ts
    assert ingestion_api._praxis is _praxis
    assert ts._praxis is _praxis

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"
PLUGIN_DIR = SCRIPT.parents[1]

# One heredoc body per `python - ... <<'PYEOF'` in the driver, in file order.
_HEREDOC_RE = re.compile(r"<<'PYEOF'[^\n]*\n(.*?)\n *PYEOF\n", re.S)


def _script_text() -> str:
    return SCRIPT.read_text()


def _block(marker: str) -> str:
    """The single embedded python block containing ``marker`` — asserting uniqueness so a test can
    never silently start pointing at a different block than the one it was written for."""
    hits = [b for b in _HEREDOC_RE.findall(_script_text()) if marker in b]
    assert len(hits) == 1, f"expected exactly one embedded python block containing {marker!r}, got {len(hits)}"
    return hits[0]


def _run_block(marker: str, argv: list[str]) -> None:
    """Execute one of the driver's embedded blocks exactly as the driver would, with ``argv`` in
    the positions the driver passes them."""
    code = compile(_block(marker), f"<af-ticket-loop:{marker}>", "exec")
    old_argv = sys.argv
    sys.argv = ["-", *argv]
    try:
        exec(code, {"__name__": "__main__"})  # noqa: S102 - executing the driver's own source is the point
    except SystemExit:
        pass
    finally:
        sys.argv = old_argv


def _requirement(cid: str = "cid-1", rid: str = "T1", **meta):
    base = {"requirement_id": rid, "build_state": "finished"}
    base.update(meta)
    return {"id": cid, "cid": cid, "meta": base}


# --------------------------------------------------------------------------- D1: the merger ingests


@pytest.fixture
def merger_stubs(monkeypatch, tmp_path):
    """Instrument every write the post-merge regress block can make, and record the order."""
    calls: dict[str, list] = {k: [] for k in (
        "ingest", "regress", "assign_class", "widen", "promote", "order")}

    fact = _requirement()
    monkeypatch.setattr(_praxis, "facts_by", lambda **kw: [fact])
    monkeypatch.setattr(_praxis, "get_fact", lambda cid, **kw: fact)

    def _regress(project, ids, detail=None, **kw):
        calls["regress"].append((project, list(ids), detail))
        calls["order"].append("regress")
        return {"count": len(ids)}

    def _ingest(project, ticket_ids, lesson_text, **kw):
        calls["ingest"].append({"project": project, "ticket_ids": list(ticket_ids),
                                "lesson": lesson_text, **kw})
        calls["order"].append("ingest")
        return {"lesson_id": "L1", "check_id": "C1", "wave_id": "W1"}

    monkeypatch.setattr(_praxis, "regress_requirements", _regress)
    monkeypatch.setattr(ingestion_api, "regress_with_ingestion", _ingest)

    def _assign(text, **kw):
        calls["assign_class"].append({"text": text, **kw})
        return {"class_id": "K1", "action": "matched", "recurrence_count": 2}

    monkeypatch.setattr(failure_taxonomy, "assign_class", _assign)
    monkeypatch.setattr(ingestion_api, "read_checks", lambda project, **kw: [
        {"id": "C1", "text": "the criterion",
         "meta": {"failure_class_id": "K1", "run": "pytest tests/x.py", "artifact_id": "A1"}}])
    monkeypatch.setattr(ingestion_api, "read_artifact", lambda aid: {"meta": {"artifact_id": aid}})
    monkeypatch.setattr(ingestion_api, "read_classes", lambda: [
        {"id": "K1", "meta": {"evidence": [{"source": "af-ticket-loop/alpha"},
                                           {"source": "af-ticket-loop/beta"}]}}])

    def _widen(check_id, project, new_scope, **kw):
        calls["widen"].append({"check_id": check_id, "project": project,
                               "new_scope": new_scope, **kw})
        return {"status": "widened", "check": {}, "proof": {}}

    def _promote(criterion, run, *, recurring_projects, **kw):
        calls["promote"].append({"criterion": criterion, "run": run,
                                 "recurring_projects": list(recurring_projects)})
        return {"status": "promoted", "check_id": "promoted-abc"}

    monkeypatch.setattr(widening, "attempt_widen", _widen)
    monkeypatch.setattr(ingestion_api, "promote_universal", _promote)
    return calls


def _verdict(tmp_path, **extra) -> str:
    body = {"verdict": "fail", "gates_green": False, "notes": "n",
            "regressed": [{"id": "T1", "reason": "the auth guard was reverted",
                           "evidence": "test_auth.py::test_guard failed", "fix": "restore it"}]}
    body.update(extra)
    p = tmp_path / "verdict.json"
    p.write_text(json.dumps(body))
    return str(p)


def test_post_merge_regression_goes_through_ingestion(merger_stubs, tmp_path):
    """R5 — the headline defect. A merger-driven regression must not be a bare
    `regress_requirements`; it must go through `regress_with_ingestion`, which lands the lesson and
    regresses in one motion. Asserted on the SHIPPED driver's own bytes."""
    _run_block("ingested = []", ["alpha", "7", _verdict(tmp_path), str(tmp_path)])

    assert len(merger_stubs["ingest"]) == 1, "the merger regressed without ingesting"
    call = merger_stubs["ingest"][0]
    assert call["project"] == "alpha"
    assert call["ticket_ids"] == ["cid-1"]
    assert call["channel"] == "machine"
    # The lesson must carry the WHY, not just an id — it is the corpus entry a future ticket reads.
    assert "T1" in call["lesson"]
    assert "the auth guard was reverted" in call["lesson"]
    assert "test_auth.py::test_guard failed" in call["lesson"]


def test_ingestion_precedes_the_detail_write_so_its_entry_is_not_clobbered(merger_stubs, tmp_path):
    """R16/E3 — ingestion writes its own regression_detail entry. The loop's audit_disposition
    write must come AFTER it and must accumulate onto a RE-READ of the ticket, or it silently
    replaces the ingestion's evidence with its own."""
    _run_block("ingested = []", ["alpha", "7", _verdict(tmp_path), str(tmp_path)])
    assert merger_stubs["order"] == ["ingest", "regress"]


def test_a_verifier_supplied_check_command_is_offered_as_the_drafted_run(merger_stubs, tmp_path):
    """The verdict's optional `check` field is what lets ingestion draft and PROVE a check rather
    than land a bare lesson — so the driver has to forward it."""
    path = _verdict(tmp_path, regressed=[{"id": "T1", "reason": "r", "evidence": "e", "fix": "f",
                                          "check": "pytest tests/test_auth.py"}])
    _run_block("ingested = []", ["alpha", "7", path, str(tmp_path)])
    assert merger_stubs["ingest"][0]["drafted_run"] == "pytest tests/test_auth.py"


def test_a_rejected_check_body_costs_the_check_and_never_the_regression(monkeypatch, merger_stubs,
                                                                        tmp_path):
    """A verifier that suggests a malformed command must not be able to prevent the regression."""
    attempts: list = []
    real = merger_stubs["ingest"]

    def _ingest(project, ticket_ids, lesson_text, **kw):
        attempts.append(kw.get("drafted_run"))
        if kw.get("drafted_run"):
            raise ingestion_api.RunBodyRejected("nope")
        real.append({"project": project, "ticket_ids": list(ticket_ids), "lesson": lesson_text, **kw})
        return {"lesson_id": "L1", "check_id": None}

    monkeypatch.setattr(ingestion_api, "regress_with_ingestion", _ingest)
    path = _verdict(tmp_path, regressed=[{"id": "T1", "reason": "r", "evidence": "e", "fix": "f",
                                          "check": "rm -rf /"}])
    _run_block("ingested = []", ["alpha", "7", path, str(tmp_path)])

    assert attempts == ["rm -rf /", None], "the driver did not retry lesson-only after the rejection"
    assert len(merger_stubs["regress"]) == 1, "the regression was lost to a bad check body"


def test_conflict_resolution_regression_also_ingests(monkeypatch, tmp_path):
    """The OTHER merger-driven regression site. Both had to be routed; routing one is half a fix."""
    ingested: list = []
    fact = _requirement(rid="T9")
    monkeypatch.setattr(_praxis, "facts_by", lambda **kw: [fact])
    monkeypatch.setattr(_praxis, "get_fact", lambda cid, **kw: fact)
    monkeypatch.setattr(_praxis, "regress_requirements", lambda *a, **k: {"count": 1})
    monkeypatch.setattr(ingestion_api, "regress_with_ingestion",
                        lambda p, t, lesson, **kw: ingested.append((p, list(t), lesson))
                        or {"lesson_id": "L", "check_id": None})

    conflicts = tmp_path / "conflicts.tsv"
    conflicts.write_text("build/T9\tT9\n")
    resolved = tmp_path / "resolved.json"
    resolved.write_text(json.dumps({"merged": ["build/T9"], "dropped_intent": [
        {"branch": "build/T9", "tickets": ["T9"], "reason": "its migration was overwritten"}]}))

    _run_block("conflict resolver: ", ["alpha", "4", str(resolved), str(tmp_path), str(conflicts)])

    assert len(ingested) == 1, "conflict-resolution regression did not ingest"
    project, ticket_ids, lesson = ingested[0]
    assert (project, ticket_ids) == ("alpha", ["cid-1"])
    assert "its migration was overwritten" in lesson


def test_merger_regression_is_not_wrapped_in_a_swallowing_except(merger_stubs, tmp_path):
    """E11 — a Praxis outage must halt the pass loudly. If someone wraps the ingestion call in a
    bare `except`, this run would report a regression it never made."""
    def _boom(*a, **k):
        raise _praxis.PraxisUnreachable("praxis is down")

    merger_stubs["ingest"].clear()
    import unittest.mock as mock
    with mock.patch.object(ingestion_api, "regress_with_ingestion", _boom):
        with pytest.raises(_praxis.PraxisUnreachable):
            _run_block("ingested = []",
                       ["alpha", "7", _verdict(tmp_path), str(tmp_path)])
    assert merger_stubs["regress"] == [], "the loop wrote a regression after ingestion failed"


# ----------------------------------------------------------- D5: recurrence widens, then promotes


def test_recurrence_drives_widening_and_universal_promotion(merger_stubs, tmp_path):
    """R14 — `attempt_widen` and `promote_universal` had no callers at all. The loop is where a
    recurrence is observable, so the loop is where they belong."""
    _run_block("ingested = []", ["alpha", "7", _verdict(tmp_path), str(tmp_path)])

    assert len(merger_stubs["assign_class"]) == 1, "no recurrence detection ran"
    assert merger_stubs["assign_class"][0]["source"] == "af-ticket-loop/alpha", (
        "the evidence source must name the project — it is what the distinct-project count reads")

    assert len(merger_stubs["widen"]) == 1, "a recurrence did not attempt a widen"
    widen = merger_stubs["widen"][0]
    assert (widen["check_id"], widen["project"], widen["new_scope"]) == ("C1", "alpha", "T1")
    assert widen["class_id"] == "K1"
    assert widen["run"] == "pytest tests/x.py"
    assert widen["bad_artifact_meta"] == {"artifact_id": "A1"}

    assert len(merger_stubs["promote"]) == 1, "a proven widen did not attempt universal promotion"
    assert merger_stubs["promote"][0]["recurring_projects"] == ["alpha", "beta"]


def test_a_first_occurrence_does_not_widen(monkeypatch, merger_stubs, tmp_path):
    """The inversion guard, at the driver level: widening is for RECURRENCE. A brand-new class must
    not reach `attempt_widen` at all."""
    monkeypatch.setattr(failure_taxonomy, "assign_class",
                        lambda text, **kw: {"class_id": "K2", "action": "minted",
                                            "recurrence_count": 1})
    _run_block("ingested = []", ["alpha", "7", _verdict(tmp_path), str(tmp_path)])
    assert merger_stubs["widen"] == []
    assert len(merger_stubs["ingest"]) == 1, "the regression still has to happen"


def test_a_failing_widen_never_costs_a_landed_regression(monkeypatch, merger_stubs, tmp_path):
    """Widening is an optimisation; the regression is the invariant. A crash in the widening pass
    must leave the already-written regression alone."""
    monkeypatch.setattr(widening, "attempt_widen",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("registry exploded")))
    _run_block("ingested = []", ["alpha", "7", _verdict(tmp_path), str(tmp_path)])
    assert len(merger_stubs["ingest"]) == 1
    assert len(merger_stubs["regress"]) == 1


# ------------------------------------------------------- D2: resolution re-evaluates, or defeats


@pytest.fixture
def resolution_fact(monkeypatch):
    fact = _requirement(regression_detail=[
        {"reason": "the guard is missing", "evidence": "test_guard failed", "check_id": "C1"},
        {"reason": "a sibling lens finding", "evidence": "other", "check_id": "C2"},
    ])
    monkeypatch.setattr(_praxis, "facts_by", lambda **kw: [fact])
    monkeypatch.setattr(_praxis, "write_build_state", lambda cid, meta, **kw: {"id": cid})
    monkeypatch.setattr(ts, "resolve_finding", lambda *a, **k: pytest.fail(
        "the driver still calls the unscoped, self-certifying ts.resolve_finding"))
    return fact


def _recheck_verdict(tmp_path, entries) -> str:
    p = tmp_path / "verdict.json"
    p.write_text(json.dumps({"verdict": "pass", "regressed": [], "findings_recheck": entries}))
    return str(p)


def test_resolution_passes_check_and_symptom_as_independent_inputs(monkeypatch, resolution_fact,
                                                                   tmp_path):
    """R17's core requirement: resolution is never inferred from the check's exit code alone. The
    driver has to hand `resolve_or_defeat` a symptom re-evaluation that is a SEPARATE input."""
    seen: list = []
    monkeypatch.setattr(resolution, "resolve_or_defeat",
                        lambda meta, check_id, **kw: seen.append((check_id, kw)) or
                        {"status": "check-defeat", "regression_detail": [], "check_id": check_id})

    path = _recheck_verdict(tmp_path, [
        {"id": "T1", "check_id": "C1", "check_passed": True, "symptom_present": True}])
    _run_block("findings_recheck unreadable", ["alpha", "7", path, str(tmp_path), "T1"])

    by_check = dict(seen)
    assert set(by_check) == {"C1", "C2"}, "resolution was not scoped per check"
    assert by_check["C1"]["check_passed"] is True
    assert by_check["C1"]["symptom_present"] is True, (
        "the verifier said the symptom persists and the driver dropped it — that is the check-defeat "
        "case R17 exists to catch")
    assert by_check["C1"]["project"] == "alpha"
    assert by_check["C1"]["ticket_id"] == "T1"


def test_a_sibling_checks_finding_is_never_stamped_by_this_checks_pass(resolution_fact, tmp_path):
    """R17 — the defect being fixed: `ts.resolve_finding(m)` stamped EVERY open finding. With real
    `resolve_or_defeat` in play, a pass on C1 must leave C2's finding open."""
    path = _recheck_verdict(tmp_path, [
        {"id": "T1", "check_id": "C1", "check_passed": True, "symptom_present": False},
        {"id": "T1", "check_id": "C2", "check_passed": False, "symptom_present": True}])
    _run_block("findings_recheck unreadable", ["alpha", "7", path, str(tmp_path), "T1"])

    details = ts.regression_details(resolution_fact["meta"])
    by_check = {d["check_id"]: d for d in details}
    assert by_check["C1"].get("resolved") is True
    assert not by_check["C2"].get("resolved"), "a sibling check's finding was stamped by C1's pass"


def test_no_recheck_report_falls_back_to_survival(resolution_fact, tmp_path):
    """The verifier may omit `findings_recheck`. Refusing to resolve at all would resurrect the
    17-round finding ping-pong, so survival still resolves — through `resolve_or_defeat`, so the
    per-check scoping holds either way."""
    path = _recheck_verdict(tmp_path, [])
    _run_block("findings_recheck unreadable", ["alpha", "7", path, str(tmp_path), "T1"])
    details = ts.regression_details(resolution_fact["meta"])
    assert all(d.get("resolved") for d in details)


def test_a_persisting_symptom_leaves_the_finding_open(resolution_fact, tmp_path):
    path = _recheck_verdict(tmp_path, [
        {"id": "T1", "check_id": "C1", "check_passed": False, "symptom_present": True},
        {"id": "T1", "check_id": "C2", "check_passed": False, "symptom_present": True}])
    _run_block("findings_recheck unreadable", ["alpha", "7", path, str(tmp_path), "T1"])
    details = ts.regression_details(resolution_fact["meta"])
    assert not any(d.get("resolved") for d in details)


# ----------------------------------------------- D1: a real passing execution proves a check

@pytest.fixture
def upgrade_spy(monkeypatch):
    """Record every `upgrade_on_first_pass` the driver makes, and let resolve_or_defeat stay real
    so the (resolved | check-defeat) branch that gates the upgrade is the module's own judgement."""
    calls: list = []
    monkeypatch.setattr(ingestion_api, "upgrade_on_first_pass",
                        lambda check_id, project, passed, **kw:
                        calls.append((check_id, project, passed)) or
                        {"meta": {"proof_status": "proven", "enforcement_state": "gating"}})
    return calls


def test_a_verifier_reported_pass_upgrades_the_check_to_proven(monkeypatch, resolution_fact,
                                                               upgrade_spy, tmp_path):
    """R6/R10/R20a — `upgrade_on_first_pass` is the ONLY mechanism by which an /af-learn check,
    which is inserted GATING but `proof_status="unproven"`, ever stops being flagged unproven. It
    had zero production callers, so the flag never cleared for anyone. The verifier re-running a
    named check against the merged tree and reporting it green IS the first real pass."""
    path = _recheck_verdict(tmp_path, [
        {"id": "T1", "check_id": "C1", "check_passed": True, "symptom_present": False}])
    _run_block("findings_recheck unreadable", ["alpha", "7", path, str(tmp_path), "T1"])

    assert upgrade_spy == [("C1", "alpha", True)], (
        "a check that was re-run for real and passed was never upgraded — R20a is dead again")


def test_a_check_defeat_demotes_and_never_upgrades(monkeypatch, resolution_fact, upgrade_spy,
                                                   tmp_path):
    """The contradiction guard. check green + symptom still present is a CHECK-DEFEAT: R17 demotes
    that check. Marking the same check `proven` in the same pass would assert both at once.

    `resolve_or_defeat` is stubbed to its DEFEAT verdict rather than run for real: the real one
    pins a repro artifact by minting a git ref in `repo_path`, which is a bare tmp dir here. What is
    under test is the driver's branch on the returned status, and that is exactly what this drives."""
    monkeypatch.setattr(resolution, "resolve_or_defeat",
                        lambda meta, check_id, **kw: {
                            "status": "check-defeat" if kw.get("symptom_present") else "resolved",
                            "regression_detail": meta.get(ts.M_REGRESSION_DETAIL) or [],
                            "check_id": check_id})
    path = _recheck_verdict(tmp_path, [
        {"id": "T1", "check_id": "C1", "check_passed": True, "symptom_present": True},
        {"id": "T1", "check_id": "C2", "check_passed": False, "symptom_present": True}])
    _run_block("findings_recheck unreadable", ["alpha", "7", path, str(tmp_path), "T1"])

    assert upgrade_spy == [], "a defeated check was promoted to proven"


def test_mere_survival_never_proves_a_check(resolution_fact, upgrade_spy, tmp_path):
    """The fallback branch ASSUMES check_passed=True when the verifier reported no recheck at all.
    That is an inference from survival, not an execution, and R20a requires a REAL non-drafting
    execution — so it must resolve the finding and prove nothing."""
    path = _recheck_verdict(tmp_path, [])
    _run_block("findings_recheck unreadable", ["alpha", "7", path, str(tmp_path), "T1"])

    details = ts.regression_details(resolution_fact["meta"])
    assert all(d.get("resolved") for d in details), "the survival fallback stopped resolving"
    assert upgrade_spy == [], "survival was accepted as proof a check executed and passed"


def test_a_failed_upgrade_never_costs_the_resolution(monkeypatch, resolution_fact, tmp_path):
    """The upgrade is bookkeeping; stamping the finding resolved is the invariant that broke a
    17-round ping-pong. A Praxis hiccup in the upgrade may not undo it."""
    writes: list = []
    monkeypatch.setattr(_praxis, "write_build_state",
                        lambda cid, meta, **kw: writes.append(meta) or {"id": cid})
    monkeypatch.setattr(ingestion_api, "upgrade_on_first_pass",
                        lambda *a, **k: (_ for _ in ()).throw(
                            _praxis.PraxisUnreachable("praxis is down")))
    path = _recheck_verdict(tmp_path, [
        {"id": "T1", "check_id": "C1", "check_passed": True, "symptom_present": False}])
    _run_block("findings_recheck unreadable", ["alpha", "7", path, str(tmp_path), "T1"])

    assert writes, "the resolution write was lost to a failing upgrade"
    by_check = {d["check_id"]: d for d in writes[-1][ts.M_REGRESSION_DETAIL]}
    assert by_check["C1"].get("resolved") is True


# --------------------------------------------------------- D6: the box worktree registry exists


def test_the_driver_populates_the_box_worktree_registry(tmp_path):
    """`widening.resolve_sibling_worktree` reads $BOX_WORKTREE_REGISTRY and treats absent as
    "sibling unavailable", so with nothing populating it `attempt_widen` can only ever PARK. The
    driver is the only process that knows the box's layout, so it builds it."""
    state = tmp_path / "state"
    (state / "beta-build" / ".git").mkdir(parents=True)
    (state / "gamma" / ".git").mkdir(parents=True)
    wt = state / "alpha-build"
    (wt / ".git").mkdir(parents=True)

    out = subprocess.run(
        [sys.executable, "-", "alpha", str(wt), str(state)],
        input=_block("this box's build-checkout conventions"),
        capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    registry = json.loads(out.stdout)
    assert registry["alpha"] == str(wt), "the one entry the driver actually knows is missing"
    assert registry["beta"] == str(state / "beta-build")
    assert registry["gamma"] == str(state / "gamma")
    # And it must be usable by the consumer, not merely well-formed.
    assert widening.resolve_sibling_worktree("alpha", registry=registry) == wt


# ------------------------------------------------------------- D3/D4: the driver's own plumbing


def test_af_retro_flags_imports_under_the_drivers_own_pythonpath():
    """The 2026-08-07 failure exactly: `python -m agent_factory.af_retro` died on
    `ModuleNotFoundError: No module named 'hooks'` under THIS script's PYTHONPATH, and `|| true`
    hid it. Reproduced here with the driver's own path construction — including the leading
    package root the export now carries — and no convenient cwd."""
    pythonpath = ":".join(str(p) for p in (PLUGIN_DIR, PLUGIN_DIR / "hooks", PLUGIN_DIR / "src"))
    out = subprocess.run(
        [sys.executable, "-c", "import agent_factory.af_retro; print('ok')"],
        cwd=str(PLUGIN_DIR.parent), env={"PATH": "/usr/bin:/bin", "PYTHONPATH": pythonpath,
                                         "HOME": "/tmp"},
        capture_output=True, text=True,
    )
    assert out.returncode == 0, f"af_retro does not import under the driver's PYTHONPATH:\n{out.stderr}"


# ------------------------------------------------------- D2: the EXIT trap really surfaces flags
#
# This block used to be four `assert ... in text` / `re.search` lines. A verifier replaced the one
# real call site (`af_surface_flags` inside `af_cleanup_on_exit`) with `: # MUTATED` and the whole
# file still passed: the regex `af_cleanup_on_exit\(\)\{.*?af_surface_flags.*?\n\}` matched the
# FUNCTION DEFINITION 26 lines further down, because `.*?` happily spans the gap. A test that passes
# with the fix deleted is not a test.
#
# So the shell is now HARNESSED. The driver's real bytes — from `af_cleanup_on_exit(){` through its
# `trap` line — are sliced out of the shipped script and sourced into a bash subprocess whose
# collaborators (`say`, `sweep_worktrees`, `reap_branches`, `$PY`) are stubs that record what
# happened. Then the subprocess exits non-zero, and the assertion is on the OBSERVABLE EFFECT:
# `agent_factory.af_retro --flags <project>` was really executed, on a failing exit. Delete the call
# site and nothing is recorded, which `test_the_flag_harness_is_not_vacuous` proves by doing exactly
# the mutation the verifier did.

_TRAP_SLICE_RE = re.compile(
    r"^af_cleanup_on_exit\(\)\{.*?^trap af_cleanup_on_exit EXIT INT TERM$", re.S | re.M)


def _trap_slice(text: str | None = None) -> str:
    """The shipped driver's cleanup/flag-surfacing region, verbatim, including its trap line."""
    hit = _TRAP_SLICE_RE.search(text if text is not None else _script_text())
    assert hit, "af_cleanup_on_exit / trap region not found in the shipped driver"
    return hit.group(0)


def _run_trap_harness(tmp_path, *, slice_text: str, exit_code: int = 7, py_rc: int = 0,
                      signal_it: bool = False):
    """Source the driver's real trap region with stubbed collaborators, then exit `exit_code`.

    Returns (returncode, log_text, recorded_argv_lines)."""
    home = tmp_path / f"h{exit_code}{py_rc}{int(signal_it)}{len(slice_text)}"
    home.mkdir(parents=True, exist_ok=True)
    wt = home / "wt"
    (wt / ".git").mkdir(parents=True, exist_ok=True)
    log = home / "loop.log"
    argv_record = home / "py-argv.txt"

    fake_py = home / "fake-python"
    fake_py.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{argv_record}"\n'
        'echo "flag: check C1 SUSPENDED — 3 consecutive no-relevant-change regressions"\n'
        f"exit {py_rc}\n")
    fake_py.chmod(0o755)

    harness = home / "harness.sh"
    harness.write_text("\n".join([
        "#!/usr/bin/env bash",
        "set -e",
        'PROJECT="alpha"',
        f'WT="{wt}"',
        f'LOG="{log}"',
        f'PY="{fake_py}"',
        'PYTHONPATH="/nonexistent"',
        'say(){ echo "$*" >> "$LOG"; }',
        'sweep_worktrees(){ echo "STUB sweep_worktrees" >> "$LOG"; }',
        'reap_branches(){ echo "STUB reap_branches" >> "$LOG"; }',
        slice_text,
        # The interesting exits are the ones that are NOT the happy path: this is where the old
        # placement (a bare call on the last line, after `af_assert_no_stragglers`) surfaced nothing.
        'kill -TERM $$' if signal_it else f"exit {exit_code}",
        "",
    ]))
    harness.chmod(0o755)

    out = subprocess.run(["/usr/bin/env", "bash", str(harness)], capture_output=True, text=True,
                         cwd=str(tmp_path))
    return (out.returncode,
            log.read_text() if log.exists() else "",
            argv_record.read_text().splitlines() if argv_record.exists() else [])


def test_flag_surfacing_really_runs_on_a_failing_exit(tmp_path):
    """R24 is a PUSH guarantee: a suspension, a parking, an undraftable check or a check-defeat may
    not wait for an operator to go looking. Executed, not grepped — the driver's own trap region is
    sourced and the process exits 7 (the straggler exit, which is exactly where the old placement
    lost the flags)."""
    rc, log, argv = _run_trap_harness(tmp_path, slice_text=_trap_slice(), exit_code=7)

    assert argv, ("nothing invoked af_retro --flags on the EXIT trap: the flag surfacing call site "
                  "is gone or unreachable")
    assert len(argv) == 1, f"flags surfaced {len(argv)} times, must be once-only: {argv}"
    assert "-m agent_factory.af_retro --flags alpha" in argv[0], argv[0]
    assert "flag: check C1 SUSPENDED" in log, "the flag output never reached the run log"
    assert rc == 7, f"the trap changed the script's exit status to {rc}"


def test_flag_surfacing_runs_when_the_run_is_killed(tmp_path):
    """`tmux kill-session` is a TERM, and it was one of the exits that silently dropped flags."""
    _, log, argv = _run_trap_harness(tmp_path, slice_text=_trap_slice(), signal_it=True)
    assert argv, "a TERM-killed run surfaced no flags"
    assert len(argv) == 1, f"INT/TERM then EXIT surfaced flags twice: {argv}"
    assert "flag: check C1 SUSPENDED" in log


def test_a_failing_flag_surface_is_said_out_loud_not_swallowed(tmp_path):
    """`|| true` used to swallow the whole thing, which is how a subsystem that failed on EVERY run
    printed nothing for a full build. The status is still discarded — a read failure may not fail a
    completed run — but the failure has to be LOUD."""
    rc, log, argv = _run_trap_harness(tmp_path, slice_text=_trap_slice(), exit_code=0, py_rc=3)
    assert argv, "the flags call did not run at all"
    assert "WARNING: pending-flag surfacing FAILED with status 3" in log, log
    assert rc == 0, "a failed flag read took the whole run down with it"


def test_the_flag_harness_is_not_vacuous(tmp_path):
    """The anti-vacuity proof, and the reason this block was rewritten: perform the EXACT mutation
    the verifier performed — replace the single `af_surface_flags` call site with `: # MUTATED` —
    and the harness above must record nothing. If this test ever fails, the three tests above are
    decorative again."""
    original = _trap_slice()
    mutated = original.replace("\n  af_surface_flags\n", "\n  : # MUTATED\n", 1)
    assert mutated != original, (
        "the call site `af_surface_flags` no longer appears inside af_cleanup_on_exit's body at "
        "the shape this mutation targets — re-point the mutation at the real call site")

    _, log, argv = _run_trap_harness(tmp_path, slice_text=mutated, exit_code=7)
    assert argv == [], ("the flags call still ran with its only call site deleted — something ELSE "
                        "in the trap region is invoking it, so the tests above prove nothing")
    assert "flag: check C1 SUSPENDED" not in log


def test_loop_end_hooks_call_both_specified_sweeps():
    """`reprove_quiet_checks` and `sweep_near_duplicate_classes` were both specified to run off the
    loop-end hook and had no caller. Executed here against instrumented modules."""
    body = _block("[loop-end")
    assert "reprove_quiet_checks" in body
    assert "sweep_near_duplicate_classes" in body


def test_loop_end_hooks_execute_both_sweeps_and_survive_either_failing(monkeypatch, tmp_path):
    calls: list = []
    monkeypatch.setattr(ingestion_api, "reprove_quiet_checks",
                        lambda project, **kw: calls.append(("reprove", project, kw)) or [])
    monkeypatch.setattr(failure_taxonomy, "sweep_near_duplicate_classes",
                        lambda **kw: calls.append(("sweep", kw)) or [])
    _run_block("[loop-end", ["alpha", str(tmp_path), "round #3"])
    assert [c[0] for c in calls] == ["reprove", "sweep"]
    assert calls[0][2]["healthy_repo_path"] == str(tmp_path)

    # A failing re-prove must not stop the near-dup sweep, and neither may propagate.
    calls.clear()
    monkeypatch.setattr(ingestion_api, "reprove_quiet_checks",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    _run_block("[loop-end", ["alpha", str(tmp_path), "round #3"])
    assert [c[0] for c in calls] == ["sweep"]


def test_loop_end_hooks_are_dispatched_off_the_critical_path():
    """A slow re-prove sweep must not be able to stall a build: the driver backgrounds it and never
    waits on it."""
    text = _script_text()
    assert re.search(r"af_loop_end_hooks\(\)\{.*?\} >>\"\$LOG\" 2>&1 &", text, re.S), (
        "the loop-end hooks are not backgrounded")
    assert "af_loop_end_hooks \"round #$round\"" in text, "nothing calls the loop-end hooks"
    assert "wait" not in re.search(r"af_loop_end_hooks\(\)\{(.*?)\n\}", text, re.S).group(1)


_LOOPEND_SLICE_RE = re.compile(r"^AF_LOOPEND_TIMEOUT_S=.*?^af_loop_end_hooks\(\)\{.*?^\}$",
                               re.S | re.M)


def test_the_round_loop_really_dispatches_the_loop_end_hooks(tmp_path):
    """`test_loop_end_hooks_are_dispatched_off_the_critical_path` asserts the CALL SITE as a string,
    which is the same shape of test D2 caught being vacuous. So the shell runs here too: the
    driver's real `af_loop_end_hooks` definition is sourced, invoked exactly as the round loop
    invokes it, and the assertion is that a `$PY` subprocess was really spawned with the sweep block
    on stdin. Sourcing DEFINES the function; only the round loop's own call line executes it."""
    hit = _LOOPEND_SLICE_RE.search(_script_text())
    assert hit, "af_loop_end_hooks region not found"
    call = next(line.strip() for line in _script_text().splitlines()
                if line.strip().startswith("af_loop_end_hooks ") and "round" in line)

    home = tmp_path / "loopend"
    (home / "wt").mkdir(parents=True)
    log = home / "loop.log"
    stdin_capture = home / "py-stdin.txt"
    fake_py = home / "fake-python"
    fake_py.write_text("#!/bin/sh\ncat >> " + str(stdin_capture) + "\necho ran-the-sweeps\n")
    fake_py.chmod(0o755)

    harness = home / "harness.sh"
    harness.write_text("\n".join([
        "#!/usr/bin/env bash", "set -e",
        'PROJECT="alpha"', f'WT="{home / "wt"}"', f'LOG="{log}"', f'PY="{fake_py}"',
        'round=3', 'say(){ echo "$*" >> "$LOG"; }',
        hit.group(0),
        call,          # verbatim from the round loop — not a paraphrase
        "wait",        # the harness waits; the DRIVER must not, which the assertion below checks
        ""]))
    harness.chmod(0o755)
    out = subprocess.run(["/usr/bin/env", "bash", str(harness)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr

    assert stdin_capture.exists(), "the loop-end hooks never spawned their python block"
    body = stdin_capture.read_text()
    assert "reprove_quiet_checks" in body and "sweep_near_duplicate_classes" in body
    assert "ran-the-sweeps" in log.read_text(), "the sweep output did not land in the run log"
    assert "loop-end hooks dispatched off-critical-path" in log.read_text()
    # And the driver's own body must contain no `wait`, or "backgrounded" is a fiction.
    assert re.search(r"\bwait\b", re.search(r"af_loop_end_hooks\(\)\{(.*?)\n\}", _script_text(),
                                            re.S).group(1)) is None


def test_pythonpath_export_carries_the_package_root():
    """`from hooks import _praxis` needs the directory CONTAINING hooks/ on the path, which the
    two module-level entries do not provide."""
    line = next(s for s in _script_text().splitlines() if s.startswith("export PYTHONPATH="))
    assert line.startswith('export PYTHONPATH="$AF_PLUGIN_DIR:'), line


def test_the_driver_no_longer_regresses_without_ingesting():
    """A belt-and-braces guard over the whole file: every merger-driven `regress_requirements` call
    site must be accompanied by an ingestion call in the same block."""
    for body in _HEREDOC_RE.findall(_script_text()):
        if "regress_requirements" not in body:
            continue
        # The two MERGER-driven sites, and only those: the finding guard's zero-commit regression is
        # a different failure class and is not in this contract.
        if not any(m in body for m in ("post-merge-verification", "conflict-resolution")):
            continue
        assert "regress_with_ingestion" in body, (
            "a merger-driven regression site writes regress_requirements with an "
            "audit_disposition and never ingests:\n" + body[:800])
