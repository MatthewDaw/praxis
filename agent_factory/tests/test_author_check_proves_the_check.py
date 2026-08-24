"""Authoring a check must RUN it. Nothing used to.

Three build blockers on this box were checks that could not pass, ever. Each cost a full round to
discover, and each blamed a worker for failing a gate that was never satisfiable:

  * an INVERTED predicate -- `grep -rL <pattern> <dir>` exits 1 exactly when every file matches,
    i.e. exactly when the invariant it guards HOLDS. Green only once the invariant broke.
  * a check gating PRE-EXISTING failures -- `make check-factory` over 25 failures that predated it
    and belonged to nobody.
  * an ABSENCE invariant the run-body grammar cannot express: there is no shell, so no `!`, `&&`
    or `|`, and what got written was a command that ran and asserted something else.

They share one observable property: run them and they are RED, on a healthy tree, at the moment
they are authored. That is the cheapest possible time to notice and nobody was looking.

The escape hatch is deliberately explicit, because "it's supposed to be red" is also precisely what
a broken check looks like, and only the author can tell the difference.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_factory import ingestion_api


@pytest.fixture
def authored(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture writes instead of performing them, so a REFUSAL is observable as 'nothing written'."""
    written: list[dict[str, Any]] = []

    def fake_write_check(text, project, *, meta=None, source=None, snapshot=None):
        written.append({"text": text, "project": project, "meta": dict(meta or {})})
        return {"id": f"fact-{len(written)}", "action": "added", "meta": dict(meta or {})}

    monkeypatch.setattr(ingestion_api, "write_check", fake_write_check)
    monkeypatch.setattr(ingestion_api, "_require_authenticated", lambda identity=None: "tester")
    return written


# ---------------------------------------------------------------------------- the happy contract --

def test_a_green_check_is_authored_and_records_that_it_was_proven(authored):
    out = ingestion_api.plan_time_author_check(
        "the suite runs", "proj", run="pytest --version")

    assert out["action"] == "added"
    meta = authored[0]["meta"]
    assert meta["proof_status"] == "proven", "a body that PASSED is proven, not merely exempt"
    assert meta["authoring_proof"]["passed"] is True
    assert meta["authoring_proof"]["exit_code"] == 0


def test_a_graded_check_has_no_body_to_run_and_stays_exempt(authored):
    ingestion_api.plan_time_author_check(
        "the diff is minimal", "proj",
        rubric={"axes": [{"name": "minimalism", "threshold": 0.8}]})

    meta = authored[0]["meta"]
    assert meta["proof_status"] == "exempt"
    assert "authoring_proof" not in meta, "there is nothing to execute for a judge-scored check"


# --------------------------------------------------------------------------------- the refusals --

def test_an_already_red_check_is_refused_and_nothing_is_written(authored):
    with pytest.raises(ingestion_api.CheckIsAlreadyRed):
        ingestion_api.plan_time_author_check(
            "the suite passes", "proj", run="pytest --definitely-not-a-real-flag")

    assert authored == [], "a refused check must not reach Praxis at all"


def test_an_inverted_absence_check_is_caught(authored, tmp_path):
    """BLOCKERS 1 and 3, reproduced together, because in practice they arrive together.

    The invariant is an ABSENCE: "no file contains TODO". The run-body grammar cannot express it —
    there is no shell, so no `!`, `&&` or `|` to negate with — so what gets authored is the bare
    search, whose exit code is INVERTED with respect to the invariant: 0 when TODOs exist, 1 when
    the tree is clean. It is therefore red on a healthy tree and would only go green once the
    invariant broke, which is the precise opposite of a guard.

    (The original blocker was spelled `grep -rL`. This test does not use it: GNU grep's exit status
    under -L turned out to differ between the ugrep wrapper on this box's interactive shell and the
    real binary a subprocess gets, so a test built on it would assert the environment rather than
    the defect.)
    """
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")

    with pytest.raises(ingestion_api.CheckIsAlreadyRed) as err:
        ingestion_api.plan_time_author_check(
            "no file contains a TODO", "proj", run="grep -r TODO .", cwd=str(tmp_path))

    assert authored == []
    assert "INVERTED" in str(err.value), "the message must name the shape it most likely is"


def test_a_check_gating_pre_existing_failures_is_caught(authored, tmp_path):
    """BLOCKER 2, reproduced: a suite that is red for reasons no ticket owns."""
    (tmp_path / "test_debt.py").write_text(
        "def test_pre_existing_failure():\n    assert False, 'this predates the check'\n")

    with pytest.raises(ingestion_api.CheckIsAlreadyRed) as err:
        ingestion_api.plan_time_author_check(
            "the suite is green", "proj", run="pytest -q", cwd=str(tmp_path))

    assert authored == []
    assert "PRE-EXISTING" in str(err.value)


def test_a_command_that_does_not_exist_is_red_not_an_unhandled_crash(authored):
    with pytest.raises(ingestion_api.CheckIsAlreadyRed):
        ingestion_api.plan_time_author_check(
            "the tool runs", "proj", run="make definitely-no-such-target")

    assert authored == []


def test_a_check_that_cannot_answer_in_time_is_red(authored, monkeypatch):
    """A body that hangs would hang the loop on every ticket it applies to. Red, and named."""
    proof = ingestion_api.prove_check_is_passable("python -c pass", timeout_s=0)
    # timeout_s=0 makes subprocess give up immediately; the point is the SHAPE of the verdict.
    assert proof["passed"] is False
    assert proof["exit_code"] in (124, 127, 1, 2), proof


def test_the_refusal_names_all_three_ways_to_land_there(authored):
    """The message is the whole remediation: whoever hit this is mid-plan and needs the taxonomy,
    not a stack trace."""
    with pytest.raises(ingestion_api.CheckIsAlreadyRed) as err:
        ingestion_api.plan_time_author_check(
            "x", "proj", run="pytest --definitely-not-a-real-flag")
    msg = str(err.value)
    assert "INVERTED" in msg
    assert "PRE-EXISTING" in msg
    assert "ABSENCE" in msg
    assert "expect_red=True" in msg, "and how to proceed when the red is intentional"


# ------------------------------------------------------------------------- the deliberate red-to-green

def test_expect_red_authors_the_check_and_records_why_it_was_red(authored):
    out = ingestion_api.plan_time_author_check(
        "the new endpoint answers", "proj", run="pytest --definitely-not-a-real-flag",
        expect_red=True)

    assert out["action"] == "added"
    meta = authored[0]["meta"]
    assert meta["proof_status"] == "unproven", "gating, but visibly never-observed-to-pass"
    assert meta["expected_red_at_authoring"] is True
    assert meta["authoring_proof"]["passed"] is False
    assert meta["authoring_proof"]["output"], "the evidence has to survive for a reviewer"


def test_expect_red_is_not_the_default(authored):
    """If it were, the whole gate would be decorative."""
    import inspect
    sig = inspect.signature(ingestion_api.plan_time_author_check)
    assert sig.parameters["expect_red"].default is False


# ------------------------------------------------------------------------------------ hygiene ----

def test_the_stored_evidence_is_secret_scrubbed(monkeypatch):
    """authoring_proof lands in Praxis, where every later reader of the check can see it."""
    monkeypatch.setattr(ingestion_api, "parse_run_body", lambda body: ["true"])

    def fake_run(argv, **kw):
        class R:
            returncode = 1
            stdout = "connecting with api_key=sk-abcdef0123456789abcdef\n"
            stderr = ""
        return R()

    monkeypatch.setattr(ingestion_api.subprocess, "run", fake_run)
    proof = ingestion_api.prove_check_is_passable("anything")
    assert "sk-abcdef0123456789abcdef" not in proof["output"]
    assert "[REDACTED]" in proof["output"]


def test_the_body_is_reparsed_and_never_reaches_a_shell(monkeypatch):
    """Authoring must not become the one door that executes a string. It runs an argv vector, the
    same way _default_runner does, and the same parser rejects the same bodies."""
    seen: dict[str, Any] = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["shell"] = kw.get("shell", False)
        class R:
            returncode = 0
            stdout = stderr = ""
        return R()

    monkeypatch.setattr(ingestion_api.subprocess, "run", fake_run)
    ingestion_api.prove_check_is_passable("pytest -q tests")
    assert seen["argv"] == ["pytest", "-q", "tests"]
    assert seen["shell"] is False

    with pytest.raises(ingestion_api.RunBodyRejected):
        ingestion_api.prove_check_is_passable("pytest -q /etc/passwd")


# ---------------------------------------------------------------------------------------- CLI ----

def test_the_cli_refuses_with_a_distinct_exit_code(monkeypatch, capsys):
    def boom(*a, **k):
        raise ingestion_api.CheckIsAlreadyRed("nope, INVERTED probably")

    monkeypatch.setattr(ingestion_api, "plan_time_author_check", boom)
    rc = ingestion_api.main(["author-check", "x", "--project", "p", "--run", "pytest -q"])

    assert rc == 2, "a refusal is not a transport failure; a script must be able to tell them apart"
    assert "INVERTED" in capsys.readouterr().err


def test_the_cli_exposes_the_escape_hatch(monkeypatch):
    seen: dict[str, Any] = {}

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return {"id": "fact-1", "action": "added", "meta": {"proof_status": "unproven"}}

    monkeypatch.setattr(ingestion_api, "plan_time_author_check", spy)
    assert ingestion_api.main([
        "author-check", "x", "--project", "p", "--run", "pytest -q", "--expect-red",
        "--cwd", "/tmp"]) == 0
    assert seen["expect_red"] is True
    assert seen["cwd"] == "/tmp"


def test_the_cli_reports_what_the_proof_found(monkeypatch, capsys):
    monkeypatch.setattr(ingestion_api, "plan_time_author_check", lambda *a, **k: {
        "id": "fact-1", "action": "added",
        "meta": {"proof_status": "proven", "authoring_proof": {"exit_code": 0}}})
    ingestion_api.main(["author-check", "x", "--project", "p", "--run", "pytest -q"])

    out = json.loads(capsys.readouterr().out)
    assert out["proof_status"] == "proven"
    assert out["authoring_exit_code"] == 0
