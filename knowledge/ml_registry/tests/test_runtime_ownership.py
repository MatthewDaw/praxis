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


def test_dead_spawned_intent_releases_its_lease_and_a_live_one_is_untouched(tmp_path: Path) -> None:
    """The wedge: a one-shot run spawns, exits, and leaves a `spawned` intent holding a lease.

    Nothing can ever move that intent to `terminal`, so admission refused
    `shared_isolation_namespace` forever with no process running. Reaping is one-sided on purpose --
    a pid that still exists is left alone, because pids are reused and releasing a live lease would
    let two executors run one campaign.
    """
    path = tmp_path / "ownership.json"
    coordinator = LeaseIntentCoordinator(path)
    dead, live = lease("D1", device="cpu"), lease("L1", device="cuda:0")
    coordinator.acquire(dead)
    coordinator.acquire(live)
    for cid, item, pid in (("D1", dead, 4001), ("L1", live, 4002)):
        coordinator.prepare(LaunchIntent(1, f"{cid}.attempt-1", cid, 1, "b" * 64,
                                         (item.lease_id,), f"run-{cid}", "prepared", 1))
        coordinator.transition(f"{cid}.attempt-1", state="spawned", pid=pid, pgid=pid)

    reaped = coordinator.reap_dead_intents(is_alive=lambda pid: pid != 4001)

    assert reaped == ("D1.attempt-1",)
    assert set(coordinator.leases) == {"L1"}
    assert set(coordinator.intents) == {"L1.attempt-1"}
    persisted = LeaseIntentCoordinator(path)
    assert set(persisted.leases) == {"L1"}
    assert set(persisted.intents) == {"L1.attempt-1"}


def test_a_prepared_intent_is_not_reaped_because_it_names_no_process(tmp_path: Path) -> None:
    """`prepared` is the window between acquiring the lease and spawning: its pid is None and the
    dispatching process is still very much alive. Only `spawned` names a process that can die."""
    coordinator = LeaseIntentCoordinator(tmp_path / "ownership.json")
    item = lease("P1", device="cpu")
    coordinator.acquire(item)
    coordinator.prepare(LaunchIntent(1, "P1.attempt-1", "P1", 1, "c" * 64,
                                     (item.lease_id,), "run-P1", "prepared", 1))

    assert coordinator.reap_dead_intents(is_alive=lambda _pid: False) == ()
    assert set(coordinator.leases) == {"P1"}
