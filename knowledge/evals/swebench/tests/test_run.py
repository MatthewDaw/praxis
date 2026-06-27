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

import pytest

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

    monkeypatch.setattr(run_mod, "_load_instances", lambda n, m, **kw: [_Inst()])

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

    monkeypatch.setattr(run_mod, "_load_instances", lambda n, m, **kw: [])
    rc = run.main(["--instances", "1", "--trials", "1"])
    assert rc == 1
    assert "no instances selected" in capsys.readouterr().err


# --- parallel-safety: worktree pool + grade concurrency cap / dedup ---------- #

class _Inst:  # minimal stand-in: only instance_id is read by the wrappers under test
    def __init__(self, iid):
        self.instance_id = iid


def test_worktree_pool_hands_out_distinct_trees_and_reclaims():
    paths = [Path("wt-0"), Path("wt-1")]
    pool = run.WorktreePool(paths)
    a = pool.acquire()
    b = pool.acquire()
    assert {a, b} == set(paths)  # two concurrent borrowers get distinct trees
    pool.release(a)
    assert pool.acquire() == a   # a released tree comes back out


def test_make_grade_fn_dedupes_identical_patch_for_same_instance():
    calls = []

    def grader(instance, patch, *, run_id):
        calls.append(run_id)
        return f"result::{run_id}"

    grade_fn = run.make_grade_fn(grader, concurrency=2)
    inst = _Inst("sympy__sympy-1")
    r1 = grade_fn(inst, "IDENTICAL PATCH")
    r2 = grade_fn(inst, "IDENTICAL PATCH")  # byte-identical → cached, not re-graded
    assert r1 == r2 and len(calls) == 1
    grade_fn(inst, "A DIFFERENT PATCH")     # distinct patch → a real second grade
    assert len(calls) == 2


def test_make_grade_fn_caps_concurrent_grades():
    import threading
    import time

    live = {"now": 0, "max": 0}
    guard = threading.Lock()
    hold = threading.Event()

    def grader(instance, patch, *, run_id):
        with guard:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        hold.wait(2)  # keep the grade "open" so overlap is observable (bounded so no hang)
        with guard:
            live["now"] -= 1
        return run_id

    grade_fn = run.make_grade_fn(grader, concurrency=2)
    # 4 DISTINCT patches (4 distinct run_ids) would all grade at once but the cap is 2.
    threads = [threading.Thread(target=grade_fn, args=(_Inst("i"), f"patch-{i}"))
               for i in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.25)  # let them pile against the semaphore
    assert live["max"] == 2  # never more than the cap, and it did reach it
    hold.set()
    for t in threads:
        t.join()


# --- parallel ingest pre-loop: warmup-first + bounded fan-out --------------- #

def test_prepare_instances_covers_all_in_order_when_serial():
    seen = []

    def prep(inst):
        seen.append(inst)
        return (inst, "result")

    out = run._prepare_instances(["a", "b", "c"], prepare_one=prep, ingest_workers=1)
    assert seen == ["a", "b", "c"]          # serial path runs each once, in order
    assert [o[0] for o in out] == ["a", "b", "c"]


def test_prepare_instances_warms_up_first_then_caps_fanout():
    import threading
    import time

    live = {"now": 0, "max": 0}
    guard = threading.Lock()
    hold = threading.Event()
    order = []

    def prep(inst):
        with guard:
            order.append(inst)
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        if inst != "warmup":  # the warmup returns at once; the rest block to expose overlap
            hold.wait(2)
        with guard:
            live["now"] -= 1
        return (inst,)

    insts = ["warmup", "a", "b", "c", "d"]
    box = {}

    def go():
        box["out"] = run._prepare_instances(insts, prepare_one=prep, ingest_workers=2)

    t = threading.Thread(target=go)
    t.start()
    time.sleep(0.25)
    assert order[0] == "warmup"   # the org-creating instance ran alone, before any fan-out
    assert live["max"] == 2       # the rest fan out but never exceed ingest_workers
    hold.set()
    t.join()
    assert {o[0] for o in box["out"]} == set(insts)  # every instance prepared


def test_prepare_instances_propagates_worker_exception():
    def prep(inst):
        if inst == "bad":
            raise ConnectionError("backend down")
        return (inst,)

    # 'bad' is in the fanned-out remainder; the first worker exception surfaces to the caller
    # (which translates connection errors into the friendly praxis-up message).
    with pytest.raises(ConnectionError):
        run._prepare_instances(["ok", "bad", "x"], prepare_one=prep, ingest_workers=2)
