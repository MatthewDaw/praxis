import pytest

from knowledge.ml_registry.scheduler import JobState, PortfolioError, ResourceProfile, schedule


CPU = {"cpus": 4, "gpus": 0, "ram_gb": 16, "disk_gb": 100, "wall_time_minutes": 60}
GPU = {"cpus": 8, "gpus": 1, "gpu_vram_gb": 24, "ram_gb": 32, "disk_gb": 100, "wall_time_minutes": 120}


def campaign(cid, *, priority=100, resources=CPU, **extra):
    return {"id": cid, "priority": priority, "resources": resources,
            "command": ["train", cid], **extra}


def test_dependency_and_priority_produce_deterministic_frontier():
    campaigns = [campaign("base", priority=20), campaign("soccer", priority=1), campaign("value")]
    states = {"value": JobState("value", "blocked", message="waiting on base:fit")}
    result = schedule(campaigns, states, {"cpus": 8, "ram_gb": 32, "disk_gb": 200}, max_concurrency=2)
    assert [job.campaign_id for job in result.jobs] == ["soccer", "base"]
    assert result.blocked["value"] == "waiting on base:fit"


def test_completed_dependency_unlocks_child():
    campaigns = [campaign("base"), campaign("child")]
    result = schedule(campaigns, {"base": JobState("base", "completed")}, CPU, max_concurrency=1)
    assert [job.campaign_id for job in result.jobs] == ["child"]


def test_failed_optional_branch_does_not_block_unrelated_work():
    campaigns = [campaign("optional"), campaign("child"), campaign("soccer")]
    states = {"optional": JobState("optional", "failed", attempt=1),
              "child": JobState("child", "blocked", message="producer optional failed")}
    result = schedule(campaigns, states, CPU, max_concurrency=1)
    assert [job.campaign_id for job in result.jobs] == ["soccer"]
    assert result.blocked["optional"] == "retry budget exhausted"
    assert result.blocked["child"] == "producer optional failed"


def test_external_blocker_is_not_overridden_by_satisfied_dependencies():
    campaigns = [campaign("labels"), campaign("other")]
    states = {"labels": JobState("labels", "blocked", message="dataset audit incomplete")}
    result = schedule(campaigns, states, CPU, max_concurrency=1)
    assert [job.campaign_id for job in result.jobs] == ["other"]
    assert result.blocked["labels"] == "dataset audit incomplete"


def test_running_jobs_consume_capacity_and_concurrency():
    campaigns = [campaign("running"), campaign("next")]
    result = schedule(campaigns, {"running": JobState("running", "running")}, CPU, max_concurrency=1)
    assert result.jobs == ()
    assert result.blocked["next"] == "concurrency limit"


def test_job_may_exactly_consume_cpu_and_gpu_capacity():
    result = schedule([campaign("exact", resources=GPU)], {}, GPU, max_concurrency=1)
    assert [job.campaign_id for job in result.jobs] == ["exact"]
    assert result.available.cpus == 0
    assert result.available.gpus == 0
    assert result.available.gpu_vram_gb == 0
    assert result.available.ram_gb == 0
    assert result.available.disk_gb == 0


def test_empty_capacity_is_valid_but_cannot_admit_a_job():
    empty = {"cpus": 0, "gpus": 0, "ram_gb": 0, "disk_gb": 0}
    result = schedule([campaign("waiting")], {}, empty, max_concurrency=1)
    assert result.jobs == ()
    assert result.blocked["waiting"] == "insufficient resources"


def test_gpu_vram_and_cost_are_admission_gates():
    campaigns = [campaign("gpu", resources=GPU, estimated_cost=4), campaign("cpu", estimated_cost=2)]
    capacity = {"cpus": 16, "gpus": 1, "gpu_vram_gb": 16, "ram_gb": 64, "disk_gb": 200}
    result = schedule(campaigns, {}, capacity, max_concurrency=2, remaining_cost=1)
    assert result.jobs == ()
    assert result.blocked == {"cpu": "cost budget", "gpu": "insufficient resources"}


def test_retry_resumes_from_latest_checkpoint():
    campaigns = [campaign("retry", max_retries=2, checkpoint_uri="s3://checkpoints/retry")]
    states = {"retry": JobState("retry", "failed", attempt=2, checkpoint_uri="s3://checkpoints/retry/2")}
    result = schedule(campaigns, states, CPU, max_concurrency=1)
    assert result.jobs[0].resume_from == "s3://checkpoints/retry/2"
    assert result.jobs[0].max_retries == 2


@pytest.mark.parametrize("campaigns, match", [
    ([campaign("a", depends_on=("missing",))], "depends_on"),
    ([campaign("a", depends_on=("b",)), campaign("b", depends_on=("a",))], "depends_on"),
    ([campaign("same"), campaign("same")], "duplicate campaign"),
])
def test_refuses_invalid_graphs(campaigns, match):
    with pytest.raises(PortfolioError, match=match):
        schedule(campaigns, {}, CPU, max_concurrency=1)


def test_backend_neutral_job_contract_carries_execution_metadata():
    item = campaign("job", resources=GPU, environment={"DATASET": "v3"}, preemptible=True,
                    timeout_minutes=90, max_retries=3, checkpoint_uri="artifact://job")
    result = schedule([item], {}, GPU, max_concurrency=1)
    job = result.jobs[0]
    assert job.command == ("train", "job")
    assert job.environment == {"DATASET": "v3"}
    assert (job.preemptible, job.timeout_minutes, job.checkpoint_uri) == (True, 90, "artifact://job")


def test_resource_profile_rejects_vram_without_gpu():
    with pytest.raises(PortfolioError, match="requires at least one GPU"):
        ResourceProfile.from_mapping({"cpus": 1, "gpus": 0, "gpu_vram_gb": 8})


@pytest.mark.parametrize("field,bad", [
    ("max_retries", None), ("max_retries", "2"), ("priority", True),
    ("estimated_cost", float("nan")), ("estimated_cost", float("inf")),
    ("preemptible", 1), ("timeout_minutes", 1.5),
])
def test_campaign_spec_coercions_fail_closed(field, bad):
    with pytest.raises(PortfolioError):
        schedule([campaign("bad", **{field: bad})], {}, CPU, max_concurrency=1)


@pytest.mark.parametrize("resources", [
    {"cpus": None}, {"cpus": "1"}, {"cpus": True}, {"ram_gb": float("nan")},
    {"ram_gb": float("inf")}, {"unknown": 1},
])
def test_resource_spec_coercions_fail_closed(resources):
    with pytest.raises(PortfolioError):
        ResourceProfile.from_mapping(resources)
