"""Offline tests for the U8 CLI wiring (``knowledge.evals.swebench.run``).

Fully offline — no backend, Docker, or ``claude``. Two things are covered, mirroring
the plan's test scenarios:

* the ``--from-records`` analysis-only path re-aggregates the committed U7 fixture,
  prints a report, and writes an out file carrying ``report`` + ``gate``;
* CLI arg parsing propagates ``--instances`` / ``--trials``, and a live run whose first
  backend call raises a connection error yields the friendly praxis-up message (return
  code 2), NOT a stack trace.

A live run is never attempted here (it needs the backend + WSL Docker).

    uv run pytest knowledge/evals/swebench/tests/test_run.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

from knowledge.evals.swebench import run

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "records.sample.json"


# --- --from-records: analysis-only, no backend / Docker ---------------------- #

def test_from_records_prints_report_and_writes_out(capsys, tmp_path):
    out = tmp_path / "RESULTS.data.json"
    rc = run.main(["--from-records", str(FIXTURE), "--out", str(out)])
    assert rc == 0

    printed = capsys.readouterr().out
    # the U7 report framing reaches stdout
    assert "ITT" in printed and "feasibility met" in printed

    # out file carries the documented keys, including the aggregated report + gate
    written = json.loads(out.read_text(encoding="utf-8"))
    assert set(written) >= {"records", "report", "gate", "rexist_map"}
    assert written["gate"]["verdict"] == "feasibility met"
    assert set(written["report"]) >= {"itt", "secondary", "hit_rate", "ingestion", "errors"}
    # the rexist_map round-trips from the fixture
    assert written["rexist_map"]["inst-A"]["r_exist"] is True


def test_from_records_does_not_touch_backend_or_docker(monkeypatch, tmp_path):
    # If the analysis path tried to construct a client or run_experiment, these would fire.
    import knowledge.evals.swebench.run as run_mod

    def _boom(*_a, **_k):  # pragma: no cover - must never be called on the offline path
        raise AssertionError("offline --from-records path must not run the live pipeline")

    monkeypatch.setattr(run_mod, "run_live", _boom)
    rc = run.main(["--from-records", str(FIXTURE), "--out", str(tmp_path / "o.json")])
    assert rc == 0


# --- CLI arg parsing: flags propagate to the live orchestrator --------------- #

def test_instances_and_trials_propagate_to_run_live(monkeypatch):
    captured = {}

    def _fake_run_live(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(run, "run_live", _fake_run_live)
    rc = run.main(["--instances", "7", "--trials", "4", "--k-rework", "2"])
    assert rc == 0
    assert captured["n_instances"] == 7
    assert captured["trials"] == 4
    assert captured["k_rework"] == 2


# --- missing backend -> friendly message, not a traceback -------------------- #

def test_connection_error_classifier_matches_socket_and_urllib():
    import urllib.error

    assert run._is_connection_error(ConnectionRefusedError("nope")) is True
    assert run._is_connection_error(ConnectionError("down")) is True
    assert run._is_connection_error(
        urllib.error.URLError(ConnectionRefusedError("refused"))
    ) is True
    # an unrelated error is not a connection error
    assert run._is_connection_error(ValueError("bad arg")) is False


def test_missing_backend_yields_friendly_message_not_traceback(monkeypatch, capsys):
    """A live run whose first backend call raises ConnectionError exits 2 with the hint."""
    import knowledge.evals.swebench.ingest as ingest_mod
    import knowledge.evals.swebench.run as run_mod

    # selection returns one fake instance without any network (we stub the loader)
    class _Inst:
        instance_id = "sympy__sympy-12345"
        base_commit = "0" * 40

    monkeypatch.setattr(run_mod, "_load_instances", lambda n, m: [_Inst()])

    # the first backend call (run_ingest, imported lazily inside run_live) refuses
    def _refuse(*_a, **_k):
        raise ConnectionError("Connection refused")

    monkeypatch.setattr(ingest_mod, "run_ingest", _refuse)

    rc = run.main(["--instances", "1", "--trials", "1"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Praxis backend not reachable" in err
    assert "praxis-up" in err
    assert "Traceback" not in err


def test_empty_selection_returns_nonzero(monkeypatch, capsys):
    import knowledge.evals.swebench.run as run_mod

    monkeypatch.setattr(run_mod, "_load_instances", lambda n, m: [])
    rc = run.main(["--instances", "1", "--trials", "1"])
    assert rc == 1
    assert "no instances selected" in capsys.readouterr().err
