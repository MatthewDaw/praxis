from pathlib import Path

import pytest

from knowledge.ml_registry.controller import ControllerError, PollResult, PortfolioController, portfolio_schedule
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
        self.results = {}
    def submit(self, job):
        self.submitted.append(job.campaign_id)
        self.results.setdefault(job.campaign_id, PollResult("running"))
        return job.campaign_id
    def poll(self, backend_job_id):
        return self.results[backend_job_id]


def test_hard_cap_allows_only_two_and_running_one_allows_one():
    portfolio = Portfolio()
    for cid in ("a", "b", "c"): ready(portfolio, cid)
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
    for cid in ("a", "b", "c"): ready(portfolio, cid)
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
    for cid in ("a", "b", "c"): ready(portfolio, cid)
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
    now[0] = 110
    assert controller.tick().started == ("a",)
    assert backend.submitted == ["a", "a"]
