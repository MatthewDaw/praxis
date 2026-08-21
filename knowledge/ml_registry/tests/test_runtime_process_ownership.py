from __future__ import annotations

import os
from pathlib import Path
import sys
import time

from knowledge.ml_registry.contracts import CampaignLease
from knowledge.ml_registry.controller import ExecutorProcessBackend, PortfolioController
from knowledge.ml_registry.portfolio import CampaignStatus, Portfolio
from knowledge.ml_registry.runtime import LeaseIntentCoordinator


def _controller(tmp_path: Path, *, seconds: float, superseded: list[tuple[str, str]]):
    portfolio = Portfolio()
    for cid in ("R1", "R2"):
        portfolio.add_campaign(cid, cid).status = CampaignStatus.READY
    coordinator = LeaseIntentCoordinator(tmp_path / "ownership.json")
    backend = ExecutorProcessBackend(tmp_path / "dispatch", coordinator=coordinator)
    specs = [{"id": cid, "command": [sys.executable, "-c", f"import time; time.sleep({seconds})"],
              "resources": {"cpus": 1}, "timeout_minutes": 1} for cid in ("R1", "R2")]

    def lease(job, token):
        now = time.time()
        return CampaignLease(1, f"lease:{job.campaign_id}", job.campaign_id, token,
                             "cpu", f"cpu:{job.campaign_id}", True, 1, "forbid", False,
                             f"state/{job.campaign_id}", f"checkout/{job.campaign_id}",
                             f"cache/{job.campaign_id}", f"ledger/{job.campaign_id}", now, now + 60)

    controller = PortfolioController(
        portfolio=portfolio, campaign_specs=specs,
        capacity={"cpus": 3, "ram_gb": 8}, backend=backend,
        state_path=tmp_path / "controller.json", coordinator=coordinator,
        lease_factory=lease, run_superseder=lambda cid, reason: superseded.append((cid, reason)),
    )
    return controller, coordinator, backend


def _groups_dead(backend: ExecutorProcessBackend) -> bool:
    for process in backend._processes.values():
        try:
            os.killpg(process.pid, 0)
        except (ProcessLookupError, PermissionError):
            continue
        return False
    return True


def test_force_kills_real_groups_supersedes_runs_and_releases_leases(tmp_path: Path) -> None:
    superseded: list[tuple[str, str]] = []
    controller, coordinator, backend = _controller(tmp_path, seconds=30, superseded=superseded)
    assert set(controller.tick().started) == {"R1", "R2"}

    report = controller.stop(mode="force")

    assert report.forced_terminations == frozenset({"R1", "R2"})
    assert coordinator.leases == {}
    assert {item[0] for item in superseded} == {"R1", "R2"}
    assert _groups_dead(backend)


def test_drain_waits_for_real_groups_without_cancelling_or_admitting(tmp_path: Path) -> None:
    superseded: list[tuple[str, str]] = []
    controller, coordinator, backend = _controller(tmp_path, seconds=.1, superseded=superseded)
    assert set(controller.tick().started) == {"R1", "R2"}

    report = controller.stop(mode="drain")

    assert report.gracefully_drained == frozenset({"R1", "R2"})
    assert superseded == []
    assert coordinator.leases == {}
    assert _groups_dead(backend)


def test_force_kills_nested_worker_in_the_executor_group(tmp_path: Path) -> None:
    child_pid = tmp_path / "child.pid"
    source = (
        "import subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        f"open({str(child_pid)!r},'w').write(str(p.pid)); "
        "time.sleep(30)"
    )
    portfolio = Portfolio()
    portfolio.add_campaign("R1", "R1").status = CampaignStatus.READY
    coordinator = LeaseIntentCoordinator(tmp_path / "ownership.json")
    backend = ExecutorProcessBackend(tmp_path / "dispatch", coordinator=coordinator)
    controller = PortfolioController(
        portfolio=portfolio,
        campaign_specs=[{"id": "R1", "command": [sys.executable, "-c", source],
                         "resources": {"cpus": 1}, "timeout_minutes": 1}],
        capacity={"cpus": 2, "ram_gb": 2}, backend=backend,
        state_path=tmp_path / "controller.json", coordinator=coordinator,
        lease_factory=lambda job, token: CampaignLease(
            1, "lease:R1", "R1", token, "cpu", "cpu:R1", True, 1, "forbid", False,
            "state/R1", "checkout/R1", "cache/R1", "registry:runs:R1",
            time.time(), time.time() + 60,
        ),
    )
    controller.tick()
    deadline = time.time() + 5
    while not child_pid.exists() and time.time() < deadline:
        time.sleep(.02)
    assert child_pid.exists()
    pid = int(child_pid.read_text())

    controller.stop(mode="force")

    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(.02)
    else:
        raise AssertionError(f"nested worker {pid} survived force-stop")
