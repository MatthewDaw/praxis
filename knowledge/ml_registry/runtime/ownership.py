"""Canonical persisted ownership for campaign leases and launch intents."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Mapping

from knowledge.ml_registry.contracts import CampaignLease, LaunchIntent


class ResourceConflict(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class StopReport:
    gracefully_drained: frozenset[str] = frozenset()
    forced_terminations: frozenset[str] = frozenset()
    retryable_interruptions: frozenset[str] = frozenset()
    orphaned: frozenset[str] = frozenset()


class ProgressTracker:
    """Typed in-memory projection of persisted run events for operator status."""

    def __init__(self) -> None:
        self._active: dict[str, int] = {}
        self._maximum: dict[str, int] = {}

    def started(self, campaign_id: str) -> None:
        active = self._active.get(campaign_id, 0) + 1
        self._active[campaign_id] = active
        self._maximum[campaign_id] = max(active, self._maximum.get(campaign_id, 0))

    def finished(self, campaign_id: str) -> None:
        self._active[campaign_id] = max(0, self._active.get(campaign_id, 0) - 1)

    def maximum_concurrent_arms(self, *, campaign_id: str) -> int:
        return self._maximum.get(campaign_id, 0)


def _atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class LeaseIntentCoordinator:
    """Single JSON projection of leases and launch intents, updated atomically."""

    def __init__(self, path: str | Path, *, clock=time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self.leases: dict[str, CampaignLease] = {}
        self.intents: dict[str, LaunchIntent] = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            self.leases = {item["campaign_id"]: CampaignLease.from_mapping(item)
                           for item in raw.get("leases", [])}
            self.intents = {item["intent_id"]: LaunchIntent.from_mapping(item)
                            for item in raw.get("intents", [])}

    def _save(self) -> None:
        _atomic(self.path, {
            "schema_version": 1,
            "leases": [lease.to_mapping() for lease in self.leases.values()],
            "intents": [intent.to_mapping() for intent in self.intents.values()],
        })

    @staticmethod
    def conflict(left: CampaignLease, right: CampaignLease) -> str | None:
        if {left.state_root, left.checkout, left.cache_root, left.ledger_path} & {
            right.state_root, right.checkout, right.cache_root, right.ledger_path,
        }:
            return "shared_isolation_namespace"
        if (left.lane == right.lane == "cpu" and left.throughput_gated
                and right.throughput_gated
                and (left.cotenancy == "forbid" or right.cotenancy == "forbid")):
            return "exclusive_cpu_throughput"
        if left.device == right.device and (
            left.exclusive or right.exclusive
            or left.cotenancy == "forbid" or right.cotenancy == "forbid"
        ):
            return "shared_cuda_0" if left.device == "cuda:0" else "shared_device"
        return None

    def acquire(self, lease: CampaignLease) -> None:
        existing = self.leases.get(lease.campaign_id)
        if existing == lease:
            return
        for other in self.leases.values():
            reason = self.conflict(lease, other)
            if reason:
                raise ResourceConflict(reason)
        self.leases[lease.campaign_id] = lease
        self._save()

    def release(self, campaign_id: str) -> None:
        self.leases.pop(campaign_id, None)
        self._save()

    def prepare(self, intent: LaunchIntent) -> None:
        previous = self.intents.get(intent.intent_id)
        if previous is not None and previous != intent:
            raise ValueError("launch intent semantic payload drifted")
        self.intents[intent.intent_id] = intent
        self._save()

    def transition(self, intent_id: str, *, state: str, pid: int | None = None,
                   pgid: int | None = None) -> LaunchIntent:
        old = self.intents[intent_id]
        value = asdict(old)
        value.update(state=state, pid=pid if pid is not None else old.pid,
                     pgid=pgid if pgid is not None else old.pgid)
        value["lease_ids"] = list(old.lease_ids)
        intent = LaunchIntent.from_mapping(value)
        self.intents[intent_id] = intent
        self._save()
        return intent
