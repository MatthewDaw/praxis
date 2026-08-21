from __future__ import annotations

from pathlib import Path

import pytest

from knowledge.ml_registry.contracts import CampaignLease, LaunchIntent
from knowledge.ml_registry.runtime import LeaseIntentCoordinator, ResourceConflict


def lease(cid: str, *, device: str, throughput: bool = False) -> CampaignLease:
    return CampaignLease(1, f"lease-{cid}", cid, f"run-{cid}",
                         "gpu" if device.startswith("cuda") else "cpu", device, True, 1,
                         "forbid", throughput, f"state/{cid}", f"checkout/{cid}",
                         f"cache/{cid}", f"ledger/{cid}", 1, 100)


def test_leases_and_intents_survive_restart_and_release(tmp_path: Path) -> None:
    path = tmp_path / "ownership.json"
    coordinator = LeaseIntentCoordinator(path)
    item = lease("R1", device="cuda:0")
    coordinator.acquire(item)
    intent = LaunchIntent(1, "R1.attempt-1", "R1", 1, "a" * 64,
                          (item.lease_id,), "run-R1", "prepared", 1)
    coordinator.prepare(intent)
    coordinator.transition(intent.intent_id, state="spawned", pid=123, pgid=123)

    restarted = LeaseIntentCoordinator(path)
    assert restarted.leases["R1"] == item
    assert restarted.intents[intent.intent_id].pgid == 123
    restarted.release("R1")
    assert LeaseIntentCoordinator(path).leases == {}


@pytest.mark.parametrize(
    ("left", "right", "reason"),
    [
        (lease("a", device="cuda:0"), lease("b", device="cuda:0"), "shared_cuda_0"),
        (lease("a", device="cpu", throughput=True),
         lease("b", device="cpu", throughput=True), "exclusive_cpu_throughput"),
    ],
)
def test_named_resource_conflict_is_machine_readable(tmp_path, left, right, reason) -> None:
    coordinator = LeaseIntentCoordinator(tmp_path / "ownership.json")
    coordinator.acquire(left)
    with pytest.raises(ResourceConflict) as exc:
        coordinator.acquire(right)
    assert exc.value.reason_code == reason
