from pathlib import Path
import os
import json
import sys
import time

import pytest

from knowledge.ml_registry.controller import (
    ControllerError,
    ExecutorProcessBackend,
    PollResult,
    PortfolioController,
    portfolio_schedule,
)
from knowledge.ml_registry.portfolio import ArtifactDependency, CampaignStatus, Portfolio
from knowledge.ml_registry.scheduler import JobState


RESOURCES = {"cpus": 4, "ram_gb": 8}


def spec(cid, **extra):
    return {"id": cid, "command": ["train", cid], "resources": {"cpus": 1}, **extra}


def ready(portfolio, cid, model=None, dependencies=()):
    campaign = portfolio.add_campaign(cid, model or cid, dependencies)
    campaign.status = CampaignStatus.READY
    return campaign


def artifact(portfolio, aid, model, **extra):
    values = dict(verdict="adopted", dataset_manifest_hash=f"d-{aid}",
                  split_manifest_hash=f"s-{aid}", prediction_manifest_hash=f"p-{aid}", coverage=1)
    values.update(extra)
    return portfolio.register_artifact(aid, model, **values)


def dep(aid, model, **extra):
    values = dict(upstream_model_id=model, artifact_id=aid, required_verdict="adopted",
                  dataset_manifest_hash=f"d-{aid}", split_manifest_hash=f"s-{aid}",
                  prediction_manifest_hash=f"p-{aid}", minimum_coverage=1)
    values.update(extra)
    return ArtifactDependency(**values)


class FakeBackend:
    def __init__(self):
        self.submitted = []
        self.submitted_specs = []
        self.results = {}
    def submit(self, job):
        self.submitted.append(job.campaign_id)
        self.submitted_specs.append(job)
        self.results.setdefault(job.campaign_id, PollResult("running"))
        return job.campaign_id
    def poll(self, backend_job_id):
        return self.results[backend_job_id]


def test_hard_cap_allows_only_two_and_running_one_allows_one():
    portfolio = Portfolio()
    for cid in ("a", "b", "c"):
        ready(portfolio, cid)
    first = portfolio_schedule(portfolio, [spec("a"), spec("b"), spec("c")], {}, RESOURCES)
    assert len(first.jobs) == 2
    second = portfolio_schedule(portfolio, [spec("a"), spec("b"), spec("c")],
                                {"a": JobState("a", "running")}, RESOURCES)
    assert len(second.jobs) == 1
    with pytest.raises(ControllerError, match="between 1 and 2"):
        portfolio_schedule(portfolio, [spec("a")], {}, RESOURCES, max_active=3)


def test_all_exact_dependencies_gate_downstream_and_unrelated_runs():
    portfolio = Portfolio()
    artifact(portfolio, "one", "m1")
    artifact(portfolio, "two", "m2", coverage=.5)
    ready(portfolio, "down", dependencies=[dep("one", "m1"), dep("two", "m2")])
    ready(portfolio, "other")
    result = portfolio_schedule(portfolio, [spec("down"), spec("other")], {}, RESOURCES)
    assert [job.campaign_id for job in result.jobs] == ["other"]
    assert "coverage" in result.blocked["down"]


def test_supersession_immediately_removes_downstream_frontier():
    portfolio = Portfolio()
    artifact(portfolio, "old", "up")
    artifact(portfolio, "new", "up")
    ready(portfolio, "down", dependencies=[dep("old", "up")])
    assert portfolio_schedule(portfolio, [spec("down")], {}, RESOURCES).jobs
    portfolio.supersede_artifact("old", "new")
    assert not portfolio_schedule(portfolio, [spec("down")], {}, RESOURCES).jobs


def test_controller_refills_restarts_without_duplicate_and_completes(tmp_path: Path):
    portfolio = Portfolio()
    for cid in ("a", "b", "c"):
        ready(portfolio, cid)
    backend = FakeBackend()
    kwargs = dict(portfolio=portfolio, campaign_specs=[spec("a"), spec("b"), spec("c")],
                  capacity=RESOURCES, backend=backend, state_path=tmp_path / "controller.json")
    controller = PortfolioController(**kwargs)
    assert len(controller.tick().started) == 2
    restarted = PortfolioController(**kwargs)
    assert restarted.tick().started == ()
    backend.results["a"] = PollResult("completed")
    assert restarted.tick().started == ("c",)
    backend.results["b"] = PollResult("completed")
    backend.results["c"] = PollResult("completed")
    assert restarted.tick().status == "complete"
    assert backend.submitted == ["a", "b", "c"]


def test_completion_artifact_unlocks_dependent_on_next_tick(tmp_path: Path):
    portfolio = Portfolio()
    ready(portfolio, "up")
    portfolio.add_campaign("down", "down", [dep("fit", "up")]).status = CampaignStatus.READY
    backend = FakeBackend()
    controller = PortfolioController(
        portfolio=portfolio, campaign_specs=[spec("up"), spec("down")], capacity=RESOURCES,
        backend=backend, state_path=tmp_path / "state.json",
    )
    assert controller.tick().started == ("up",)
    backend.results["up"] = PollResult("completed", artifact={
        "artifact_id": "fit", "model_id": "up", "verdict": "adopted",
        "dataset_manifest_hash": "d-fit", "split_manifest_hash": "s-fit",
        "prediction_manifest_hash": "p-fit", "coverage": 1,
    })
    assert controller.tick().started == ("down",)


def test_controller_reports_terminally_blocked_portfolio(tmp_path: Path):
    portfolio = Portfolio()
    campaign = portfolio.add_campaign("labels", "labels")
    campaign.status = CampaignStatus.BLOCKED
    result = PortfolioController(
        portfolio=portfolio, campaign_specs=[spec("labels")], capacity=RESOURCES,
        backend=FakeBackend(), state_path=tmp_path / "state.json",
    ).run(one_shot=True)
    assert result.status == "blocked"
    assert "labels" in result.blocked


def test_run_continuously_refills_without_busy_spin(tmp_path: Path):
    portfolio = Portfolio()
    for cid in ("a", "b", "c"):
        ready(portfolio, cid)
    backend = FakeBackend()
    sleeps = []
    def sleeper(interval):
        sleeps.append(interval)
        for cid in list(backend.results):
            backend.results[cid] = PollResult("completed")
    result = PortfolioController(
        portfolio=portfolio, campaign_specs=[spec("a"), spec("b"), spec("c")],
        capacity=RESOURCES, backend=backend, state_path=tmp_path / "state.json",
    ).run(poll_interval=.25, sleeper=sleeper)
    assert result.status == "complete"
    assert backend.submitted == ["a", "b", "c"]
    assert sleeps == [.25, .25]


def test_failed_job_waits_for_backoff_then_retries(tmp_path: Path):
    portfolio = Portfolio()
    ready(portfolio, "a")
    backend = FakeBackend()
    now = [100.0]
    controller = PortfolioController(
        portfolio=portfolio, campaign_specs=[spec("a", max_retries=1)], capacity=RESOURCES,
        backend=backend, state_path=tmp_path / "state.json", retry_backoff_seconds=10,
        clock=lambda: now[0],
    )
    controller.tick()
    backend.results["a"] = PollResult("failed", message="preempted")
    assert controller.tick().status == "waiting"
    now[0] = controller.records["a"].next_retry_at
    assert controller.tick().started == ("a",)
    assert backend.submitted == ["a", "a"]


def test_unknown_poll_state_fails_closed_instead_of_stalling(tmp_path: Path):
    portfolio = Portfolio()
    ready(portfolio, "a")
    backend = FakeBackend()
    controller = PortfolioController(
        portfolio=portfolio, campaign_specs=[spec("a")], capacity=RESOURCES,
        backend=backend, state_path=tmp_path / "state.json", retry_backoff_seconds=10,
        clock=lambda: 100,
    )
    controller.tick()
    backend.results["a"] = PollResult("garbage")
    assert controller.tick().status == "waiting"
    assert "unknown backend state" in controller.records["a"].message


def test_artifact_extra_fields_are_ignored_but_wrong_model_fails(tmp_path: Path):
    portfolio = Portfolio()
    ready(portfolio, "a", model="expected")
    backend = FakeBackend()
    controller = PortfolioController(
        portfolio=portfolio, campaign_specs=[spec("a")], capacity=RESOURCES,
        backend=backend, state_path=tmp_path / "state.json", retry_backoff_seconds=10,
        clock=lambda: 100,
    )
    controller.tick()
    backend.results["a"] = PollResult("completed", artifact={
        "artifact_id": "fit", "model_id": "wrong", "verdict": "adopted",
        "dataset_manifest_hash": "d", "split_manifest_hash": "s",
        "prediction_manifest_hash": "p", "coverage": 1, "future_field": "safe",
    })
    assert controller.tick().status == "waiting"
    assert "model_id" in controller.records["a"].message


def test_failed_checkpoint_is_forwarded_to_retry_resume_from(tmp_path: Path):
    portfolio = Portfolio()
    ready(portfolio, "a")
    backend = FakeBackend()
    now = [100.0]
    controller = PortfolioController(
        portfolio=portfolio, campaign_specs=[spec("a", max_retries=1)], capacity=RESOURCES,
        backend=backend, state_path=tmp_path / "state.json", retry_backoff_seconds=1,
        clock=lambda: now[0],
    )
    controller.tick()
    backend.results["a"] = PollResult("failed", checkpoint_uri="artifact://checkpoint/1")
    controller.tick()
    now[0] = controller.records["a"].next_retry_at
    controller.tick()
    assert backend.submitted == ["a", "a"]
    assert backend.submitted_specs[-1].resume_from == "artifact://checkpoint/1"


def test_real_executor_process_publishes_artifact_and_unlocks_downstream(tmp_path: Path):
    portfolio = Portfolio()
    ready(portfolio, "up", model="up")
    portfolio.add_campaign("down", "down", [dep("fit", "up")]).status = CampaignStatus.READY
    artifact_path = tmp_path / "produced-artifact.json"
    payload = {
        "artifact_id": "fit", "model_id": "up", "verdict": "adopted",
        "dataset_manifest_hash": "d-fit", "split_manifest_hash": "s-fit",
        "prediction_manifest_hash": "p-fit", "coverage": 1,
        "producer_campaign_id": "up",
    }
    write_artifact = (
        sys.executable, "-c",
        f"import json; open({str(artifact_path)!r}, 'w').write(json.dumps({payload!r}))",
    )
    controller = PortfolioController(
        portfolio=portfolio,
        campaign_specs=[
            spec("up", command=list(write_artifact), artifact_result_path=str(artifact_path)),
            spec("down", command=[sys.executable, "-c", "pass"]),
        ],
        capacity=RESOURCES,
        backend=ExecutorProcessBackend(tmp_path / "dispatch"),
        state_path=tmp_path / "controller.json",
    )
    assert controller.tick().started == ("up",)
    executor_state = tmp_path / "dispatch" / "up.attempt-1.state.json"
    deadline = time.time() + 5
    while time.time() < deadline:
        if executor_state.exists() and json.loads(executor_state.read_text()).get("state") != "running":
            break
        time.sleep(.02)
    assert json.loads(executor_state.read_text())["state"] == "completed"
    result = controller.tick()
    assert "fit" in portfolio.artifacts
    assert result.started == ("down",)


SLEEPER = "import time; time.sleep(30)"


def _wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(.02)
    return predicate()


def _dispatching(state_path: Path, cid: str, token: str) -> None:
    """Rewrite persisted state as it looks between _persist('dispatching') and Popen."""
    raw = json.loads(state_path.read_text())
    record = raw["records"].setdefault(cid, {"attempt": 1, "next_retry_at": 0.0,
                                             "message": None, "started_at": 0.0,
                                             "checkpoint_uri": None})
    record.update(state="dispatching", backend_job_id=token)
    state_path.write_text(json.dumps(raw))


def test_restart_after_launch_adopts_the_live_attempt_without_double_launching(tmp_path: Path):
    portfolio = Portfolio()
    ready(portfolio, "a")
    root = tmp_path / "dispatch"
    backend = ExecutorProcessBackend(root)
    state_path = tmp_path / "controller.json"
    kwargs = dict(portfolio=portfolio, capacity=RESOURCES, state_path=state_path,
                  campaign_specs=[spec("a", command=[sys.executable, "-c", SLEEPER],
                                       max_retries=3)])
    try:
        controller = PortfolioController(backend=backend, **kwargs)
        assert controller.tick().started == ("a",)
        assert _wait_for(lambda: (root / "a.attempt-1.state.json").exists())
        _dispatching(state_path, "a", "a.attempt-1")

        restarted = PortfolioController(backend=ExecutorProcessBackend(root), **kwargs)
        assert restarted.records["a"].state == "running"
        result = restarted.tick()
        assert result.started == ()
        assert result.running == ("a",)
        assert not (root / "a.attempt-2.process.json").exists()
        assert restarted.records["a"].attempt == 1
    finally:
        backend.terminate("a.attempt-1")


def test_restart_before_launch_deletes_the_record_without_burning_the_attempt(tmp_path: Path):
    portfolio = Portfolio()
    ready(portfolio, "a")
    state_path = tmp_path / "controller.json"
    state_path.write_text(json.dumps({"status": "dispatching", "updated_at": 1.0, "records": {
        "a": {"backend_job_id": "a.attempt-1", "state": "dispatching", "attempt": 1,
              "next_retry_at": 0.0, "message": None, "started_at": 1.0, "checkpoint_uri": None},
    }}))
    backend = ExecutorProcessBackend(tmp_path / "dispatch")
    controller = PortfolioController(
        portfolio=portfolio, campaign_specs=[spec("a", max_retries=0)], capacity=RESOURCES,
        backend=backend, state_path=state_path,
    )
    assert "a" not in controller.records
    controller.backend = FakeBackend()
    assert controller.tick().started == ("a",)
    assert controller.records["a"].attempt == 1


def test_restart_during_dispatch_never_retries_immediately(tmp_path: Path):
    portfolio = Portfolio()
    ready(portfolio, "a")
    state_path = tmp_path / "controller.json"
    state_path.write_text(json.dumps({"status": "dispatching", "updated_at": 1.0, "records": {
        "a": {"backend_job_id": "a.attempt-1", "state": "dispatching", "attempt": 1,
              "next_retry_at": 0.0, "message": None, "started_at": 1.0, "checkpoint_uri": None},
    }}))
    controller = PortfolioController(
        portfolio=portfolio, campaign_specs=[spec("a", max_retries=2)], capacity=RESOURCES,
        backend=FakeBackend(), state_path=state_path, retry_backoff_seconds=60,
        clock=lambda: 1000.0,
    )
    assert controller.records["a"].state == "failed"
    assert controller.records["a"].next_retry_at >= 1000.0 + 54
    assert controller.tick().status == "waiting"


def test_sigkilled_executor_is_reaped_and_reported_terminal(tmp_path: Path):
    backend = ExecutorProcessBackend(tmp_path / "dispatch")
    from knowledge.ml_registry.scheduler import JobSpec, ResourceProfile
    job = JobSpec("a", (sys.executable, "-c", "import time; time.sleep(2)"), ResourceProfile())
    token = backend.submit_prepared(job, "a.attempt-1")
    process = backend.processes[token]
    assert _wait_for(lambda: (tmp_path / "dispatch" / "a.attempt-1.state.json").exists())
    process.kill()
    process.wait(timeout=10)
    polled = backend.poll(token)
    assert polled.state == "failed"
    assert "exited with code" in polled.message
    assert process.poll() is not None


def test_executor_dying_before_state_is_failed_not_running_forever(tmp_path: Path, monkeypatch):
    import knowledge.ml_registry.controller as controller_module
    real_popen = controller_module.subprocess.Popen

    def crashing(command, **kwargs):
        return real_popen([sys.executable, "-c",
                           "import sys; sys.stderr.write('ImportError: boom'); raise SystemExit(3)"],
                          **kwargs)

    monkeypatch.setattr(controller_module.subprocess, "Popen", crashing)
    backend = ExecutorProcessBackend(tmp_path / "dispatch")
    from knowledge.ml_registry.scheduler import JobSpec, ResourceProfile
    token = backend.submit_prepared(JobSpec("a", ("train",), ResourceProfile()), "a.attempt-1")
    backend.processes[token].wait(timeout=10)
    states = [backend.poll(token).state for _ in range(5)]
    assert states == ["failed"] * 5
    assert "ImportError: boom" in backend.poll(token).message


def test_launch_timeout_fails_a_silent_executor(tmp_path: Path):
    backend = ExecutorProcessBackend(tmp_path / "dispatch", launch_timeout_seconds=0)
    from knowledge.ml_registry.scheduler import JobSpec, ResourceProfile
    job = JobSpec("a", (sys.executable, "-c", SLEEPER), ResourceProfile())
    token = backend.submit_prepared(job, "a.attempt-1")
    try:
        (tmp_path / "dispatch" / "a.attempt-1.state.json").unlink(missing_ok=True)
        polled = backend.poll(token)
        assert polled.state == "failed"
        assert "no state within" in polled.message
    finally:
        backend.terminate(token)


def test_stale_process_json_pid_reuse_is_rejected(tmp_path: Path):
    backend = ExecutorProcessBackend(tmp_path / "dispatch")
    _atomic = json.dumps({"pid": os.getpid(), "started_at": time.time(),
                          "state_path": "x", "start_token": "not-the-real-start-time"})
    (tmp_path / "dispatch" / "a.attempt-1.process.json").write_text(_atomic)
    assert backend.poll("a.attempt-1").state == "failed"


def test_schedule_failure_keeps_supervising_live_executors(tmp_path: Path, monkeypatch):
    portfolio = Portfolio()
    for cid in ("a", "b"):
        ready(portfolio, cid)
    backend = FakeBackend()
    controller = PortfolioController(
        portfolio=portfolio, campaign_specs=[spec("a"), spec("b")], capacity=RESOURCES,
        backend=backend, state_path=tmp_path / "state.json",
    )
    assert len(controller.tick().started) == 2
    import knowledge.ml_registry.controller as controller_module

    def explode(*args, **kwargs):
        raise ValueError("capacity file shrank")

    monkeypatch.setattr(controller_module, "portfolio_schedule", explode)
    result = controller.tick()
    assert result.status == "running"
    assert result.running == ("a", "b")
    assert "capacity file shrank" in result.blocked["controller"]
    sleeps = []

    def sleeper(interval):
        sleeps.append(interval)
        for cid in ("a", "b"):
            backend.results[cid] = PollResult("completed")

    outcome = controller.run(poll_interval=.01, sleeper=sleeper)
    assert outcome.status == "complete"
    assert sleeps == [.01]  # it kept polling instead of exiting on the schedule error


def test_non_mapping_artifact_fails_the_record_and_keeps_the_loop_alive(tmp_path: Path):
    portfolio = Portfolio()
    ready(portfolio, "a")
    backend = FakeBackend()
    controller = PortfolioController(
        portfolio=portfolio, campaign_specs=[spec("a", max_retries=1)], capacity=RESOURCES,
        backend=backend, state_path=tmp_path / "state.json", retry_backoff_seconds=10,
        clock=lambda: 100.0,
    )
    controller.tick()
    backend.results["a"] = PollResult("completed", artifact=["not", "a", "mapping"])
    assert controller.tick().status == "waiting"
    assert "artifact payload must be a mapping" in controller.records["a"].message
    restarted = PortfolioController(
        portfolio=portfolio, campaign_specs=[spec("a", max_retries=1)], capacity=RESOURCES,
        backend=backend, state_path=tmp_path / "state.json", clock=lambda: 100.0,
    )
    assert restarted.tick().status == "waiting"


@pytest.mark.parametrize("artifact_id", [5, ["x"], None, "  "])
def test_non_string_artifact_id_is_refused_rather_than_coerced(tmp_path: Path, artifact_id):
    portfolio = Portfolio()
    ready(portfolio, "a")
    backend = FakeBackend()
    controller = PortfolioController(
        portfolio=portfolio, campaign_specs=[spec("a", max_retries=1)], capacity=RESOURCES,
        backend=backend, state_path=tmp_path / "state.json", retry_backoff_seconds=10,
        clock=lambda: 100.0,
    )
    controller.tick()
    backend.results["a"] = PollResult("completed", artifact={
        "artifact_id": artifact_id, "model_id": "a", "verdict": "adopted",
        "dataset_manifest_hash": "d", "split_manifest_hash": "s",
        "prediction_manifest_hash": "p", "coverage": 1,
    })
    assert controller.tick().status == "waiting"
    assert "artifact_id must be a non-empty string" in controller.records["a"].message
    assert str(artifact_id) not in portfolio.artifacts


def test_checkpoint_survives_into_the_third_attempt(tmp_path: Path):
    portfolio = Portfolio()
    ready(portfolio, "a")
    backend = FakeBackend()
    now = [100.0]
    controller = PortfolioController(
        portfolio=portfolio, campaign_specs=[spec("a", max_retries=2)], capacity=RESOURCES,
        backend=backend, state_path=tmp_path / "state.json", retry_backoff_seconds=1,
        clock=lambda: now[0],
    )
    controller.tick()
    backend.results["a"] = PollResult("failed", checkpoint_uri="artifact://checkpoint/1")
    controller.tick()
    now[0] = controller.records["a"].next_retry_at
    controller.tick()
    backend.results["a"] = PollResult("failed", message="preempted again")
    controller.tick()
    now[0] = controller.records["a"].next_retry_at
    controller.tick()
    assert len(backend.submitted_specs) == 3
    assert controller.records["a"].checkpoint_uri == "artifact://checkpoint/1"
    assert backend.submitted_specs[-1].resume_from == "artifact://checkpoint/1"


def test_declared_environment_keys_are_allowlisted_for_the_executor(tmp_path: Path):
    portfolio = Portfolio()
    ready(portfolio, "a")
    marker = tmp_path / "seed.txt"
    command = [sys.executable, "-c",
               f"import os; open({str(marker)!r}, 'w').write(os.environ['CAMPAIGN_SEED'])"]
    root = tmp_path / "dispatch"
    controller = PortfolioController(
        portfolio=portfolio, capacity=RESOURCES, backend=ExecutorProcessBackend(root),
        campaign_specs=[spec("a", command=command, environment={"CAMPAIGN_SEED": "7"})],
        state_path=tmp_path / "controller.json",
    )
    assert controller.tick().started == ("a",)
    state_file = root / "a.attempt-1.state.json"
    assert _wait_for(lambda: state_file.exists()
                     and json.loads(state_file.read_text())["state"] != "running")
    state = json.loads(state_file.read_text())
    assert state["state"] == "completed", state.get("message")
    assert marker.read_text() == "7"


def test_artifact_from_a_superseded_attempt_is_refused(tmp_path: Path):
    portfolio = Portfolio()
    ready(portfolio, "a")
    backend = FakeBackend()
    controller = PortfolioController(
        portfolio=portfolio, campaign_specs=[spec("a", max_retries=1)], capacity=RESOURCES,
        backend=backend, state_path=tmp_path / "state.json", retry_backoff_seconds=10,
        clock=lambda: 100.0,
    )
    controller.tick()
    backend.results["a"] = PollResult("completed", attempt_token="a.attempt-1", artifact={
        "artifact_id": "fit", "model_id": "a", "verdict": "adopted",
        "dataset_manifest_hash": "d", "split_manifest_hash": "s",
        "prediction_manifest_hash": "p", "coverage": 1,
    })
    assert controller.tick().status == "waiting"
    assert "superseded attempt" in controller.records["a"].message
    assert "fit" not in portfolio.artifacts


def test_backend_refuses_an_artifact_written_by_a_superseded_attempt(tmp_path: Path):
    root = tmp_path / "dispatch"
    backend = ExecutorProcessBackend(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.active.json").write_text(json.dumps({"job_id": "a.attempt-2"}))
    (root / "a.attempt-1.state.json").write_text(json.dumps({
        "state": "completed", "artifact": {"artifact_id": "fit"},
    }))
    polled = backend.poll("a.attempt-1")
    assert polled.state == "failed"
    assert "superseded attempt" in polled.message


def test_redispatch_terminates_a_still_live_predecessor(tmp_path: Path):
    portfolio = Portfolio()
    ready(portfolio, "a")
    terminated = []

    class TerminatingBackend(FakeBackend):
        def submit_prepared(self, job, token):
            self.submit(job)
            self.results[token] = PollResult("running")
            return token
        def terminate(self, job_id):
            terminated.append(job_id)
            return True

    backend = TerminatingBackend()
    now = [100.0]
    controller = PortfolioController(
        portfolio=portfolio, campaign_specs=[spec("a", max_retries=1)], capacity=RESOURCES,
        backend=backend, state_path=tmp_path / "state.json", retry_backoff_seconds=1,
        clock=lambda: now[0],
    )
    controller.tick()
    backend.results["a.attempt-1"] = PollResult("failed", message="heartbeat stale")
    controller.tick()
    now[0] = controller.records["a"].next_retry_at
    controller.tick()
    assert terminated == ["a.attempt-1"]


def test_backoff_jitter_is_per_attempt_and_does_not_collide_across_ids(tmp_path: Path):
    controller = PortfolioController(
        portfolio=Portfolio(), campaign_specs=[], capacity=RESOURCES, backend=FakeBackend(),
        state_path=tmp_path / "state.json", retry_backoff_seconds=100,
    )
    assert controller._backoff("ab", 1, 0.0) != controller._backoff("ba", 1, 0.0)
    assert controller._backoff("ab", 1, 0.0) != controller._backoff("ab", 1, 5.0)
    assert controller._backoff("ab", 1, 0.0) == controller._backoff("ab", 1, 0.0)
    assert 90 <= controller._backoff("ab", 1, 0.0) <= 110
