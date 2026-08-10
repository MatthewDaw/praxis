"""The operator CLIs must DIAGNOSE a Praxis failure, never traceback at it.

Observed before this boundary existed, running `af-retro sports_analysis` from the praxis checkout:

    Traceback (most recent call last):
      ... 25 lines ...
    urllib.error.HTTPError: HTTP Error 404: Not Found
    ... during handling of the above, another exception ...
    hooks._praxis.PraxisUnreachable: Praxis GET /facts/by -> HTTP 404:
        {"detail":"unknown space 'sports_analysis'"}

Nothing was wrong. Each factory project pins its own `PRAXIS_ORG`, so the command had asked the
`praxis` org about a space that only exists under `sports-analysis` -- a one-word fix (`cd`) that
presented as a crashed tool. This is the CLI half of the same conflation the Stop-hook gates
shipped and `_gate_common.not_a_factory_project` fixed; the tests below pin that the CLIs reuse
that predicate rather than re-deriving it.

The NEGATIVE case carries the most weight: a real outage must still be reported as an outage, and a
genuine bug in this package must still raise. A boundary that tidied every exception into "could
not run" would hide programming errors behind a friendly message -- strictly worse than the
traceback it replaced.
"""

from __future__ import annotations

import pytest

from agent_factory._cli import EXIT_CANNOT_RUN, praxis_boundary
from agent_factory._hooks import _praxis

MISSING_SPACE = "Praxis GET /facts/by -> HTTP 404: {\"detail\":\"unknown space 'sports_analysis'\"}"
WRONG_ORG = ("Praxis GET /context -> HTTP 403: "
             "{\"detail\":\"API key is not scoped to org 'sports-analysis'\"}")
OUTAGE = "Praxis GET /facts/by -> HTTP 500: internal error"


def _boom(message: str):
    def raise_it() -> int:
        raise _praxis.PraxisUnreachable(message)
    return raise_it


def test_a_successful_command_passes_its_status_through():
    assert praxis_boundary("af-retro", lambda: 0) == 0
    assert praxis_boundary("af-retro", lambda: 7) == 7


@pytest.mark.parametrize("message", [MISSING_SPACE, WRONG_ORG])
def test_a_scoping_failure_explains_the_org_instead_of_tracebacking(capsys, message):
    status = praxis_boundary("af-retro", _boom(message))
    err = capsys.readouterr().err
    assert status == EXIT_CANNOT_RUN
    assert "Traceback" not in err
    # The actionable part: name the fix, not just the failure.
    assert "PRAXIS_ORG" in err
    assert "not an outage" in err


def test_a_real_outage_is_reported_as_an_outage(capsys):
    """The negative case. Reading a 500 as "you're in the wrong directory" would send an operator
    hunting a config problem that does not exist while Praxis is genuinely down."""
    status = praxis_boundary("af-retro", _boom(OUTAGE))
    err = capsys.readouterr().err
    assert status == EXIT_CANNOT_RUN
    assert "unreachable" in err
    assert "PRAXIS_ORG" not in err, "an outage was misreported as a scoping mistake"


def test_a_programming_error_still_raises():
    """A bug in the package must NOT be tidied into a clean exit -- that is how a real defect
    becomes invisible."""
    def bug() -> int:
        raise ValueError("a genuine bug")
    with pytest.raises(ValueError, match="a genuine bug"):
        praxis_boundary("af-retro", bug)


def test_af_retro_report_is_wired_to_the_boundary(capsys, monkeypatch):
    """End-to-end through the real `main`, since the boundary is only worth anything if the entry
    point actually routes through it."""
    from agent_factory import af_retro

    monkeypatch.setattr(af_retro, "read_lessons",
                        lambda *a, **k: (_ for _ in ()).throw(
                            _praxis.PraxisUnreachable(MISSING_SPACE)))
    status = af_retro.main(["sports_analysis"])
    out, err = capsys.readouterr()
    assert status == EXIT_CANNOT_RUN
    assert "Traceback" not in err and "Traceback" not in out
    assert "PRAXIS_ORG" in err


# --------------------------------------------------------- an UNINITIALISED learnings space --
#
# `af-retro --flags` is run off af-ticket-loop.sh's EXIT trap at the end of every round. The shared
# `factory-learnings` space is created lazily by the first af-ingest write, so on a project that has
# never ingested a lesson the read 404s -- and every round of the farming_analysis run (2026-08-09)
# ended with `af-retro: nothing to read in org 'farming-analysis' -- unknown space
# 'factory-learnings'` and exit 2. The flags that report parked and suspended checks -- the entire
# push-not-pull guarantee -- were never printed, because the command never got that far.
#
# "No flags yet" is a correct, complete answer to "what is pending". It is not an error.

MISSING_LEARNINGS = ("Praxis GET /facts/by -> HTTP 404: "
                     "{\"detail\":\"unknown space 'factory-learnings'\"}")


def test_flags_on_an_uninitialised_learnings_space_is_success_not_status_2(capsys, monkeypatch):
    from agent_factory import af_retro

    monkeypatch.setattr(af_retro, "read_flags",
                        lambda *a, **k: (_ for _ in ()).throw(
                            _praxis.PraxisUnreachable(MISSING_LEARNINGS)))
    status = af_retro.main(["--flags"])
    out, err = capsys.readouterr()

    assert status == 0, "an empty learnings space is an empty answer, not a failed command"
    assert "no pending flags (learnings space not initialised)" in out
    assert err == "", "nothing to diagnose: there is no fault here"


def test_a_missing_project_space_still_gets_the_scoping_diagnosis(capsys, monkeypatch):
    """The negative case. Only the SHARED learnings space is allowed to be absent; a 404 naming any
    other space is the real 'you are in the wrong directory' answer and keeps its exit 2."""
    from agent_factory import af_retro

    monkeypatch.setattr(af_retro, "read_flags",
                        lambda *a, **k: (_ for _ in ()).throw(
                            _praxis.PraxisUnreachable(MISSING_SPACE)))
    status = af_retro.main(["--flags"])
    err = capsys.readouterr().err

    assert status == EXIT_CANNOT_RUN
    assert "PRAXIS_ORG" in err


def test_flags_still_reports_a_real_outage(capsys, monkeypatch):
    from agent_factory import af_retro

    monkeypatch.setattr(af_retro, "read_flags",
                        lambda *a, **k: (_ for _ in ()).throw(_praxis.PraxisUnreachable(OUTAGE)))
    status = af_retro.main(["--flags"])
    err = capsys.readouterr().err

    assert status == EXIT_CANNOT_RUN
    assert "unreachable" in err


def test_af_ingest_is_wired_to_the_boundary(capsys, monkeypatch):
    from agent_factory import ingestion_api

    monkeypatch.setattr(ingestion_api, "read_lessons",
                        lambda *a, **k: (_ for _ in ()).throw(
                            _praxis.PraxisUnreachable(WRONG_ORG)))
    status = ingestion_api.main(["read"])
    err = capsys.readouterr().err
    assert status == EXIT_CANNOT_RUN
    assert "Traceback" not in err
    assert "PRAXIS_ORG" in err
