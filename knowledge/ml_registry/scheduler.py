"""Deterministic, backend-neutral scheduling for ML campaign portfolios.

The scheduler deliberately does not launch processes or provision machines.  It
turns a portfolio snapshot into a validated ready frontier; an executor can then
translate :class:`JobSpec` to a local, batch, or cloud backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


TERMINAL_SUCCESS = frozenset({"completed", "skipped"})
KNOWN_STATES = frozenset({"planned", "blocked", "ready", "running", "completed", "failed", "skipped"})


class PortfolioError(ValueError):
    """Raised when a portfolio cannot be scheduled safely."""


@dataclass(frozen=True)
class ResourceProfile:
    """Consumable capacity; ``gpu_vram_gb`` is aggregate VRAM, not per-device."""

    cpus: int = 1
    gpus: int = 0
    gpu_vram_gb: float = 0.0
    ram_gb: float = 1.0
    disk_gb: float = 0.0
    wall_time_minutes: int = 60

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
        *,
        allow_zero_cpus: bool = False,
    ) -> "ResourceProfile":
        """Parse resources, allowing an empty CPU pool only for capacity snapshots."""
        profile = cls(**dict(value or {}))
        numeric = (profile.cpus, profile.gpus, profile.gpu_vram_gb, profile.ram_gb,
                   profile.disk_gb, profile.wall_time_minutes)
        if (any(item < 0 for item in numeric)
                or (profile.cpus == 0 and not allow_zero_cpus)
                or profile.wall_time_minutes == 0):
            raise PortfolioError("resource quantities must be non-negative; cpus and wall_time_minutes must be positive")
        if profile.gpus == 0 and profile.gpu_vram_gb:
            raise PortfolioError("gpu_vram_gb requires at least one GPU")
        return profile

    def fits(self, available: "ResourceProfile") -> bool:
        return (
            self.cpus <= available.cpus
            and self.gpus <= available.gpus
            and self.gpu_vram_gb <= available.gpu_vram_gb
            and self.ram_gb <= available.ram_gb
            and self.disk_gb <= available.disk_gb
        )

    def subtract(self, other: "ResourceProfile") -> "ResourceProfile":
        return ResourceProfile(
            cpus=self.cpus - other.cpus,
            gpus=self.gpus - other.gpus,
            gpu_vram_gb=self.gpu_vram_gb - other.gpu_vram_gb,
            ram_gb=self.ram_gb - other.ram_gb,
            disk_gb=self.disk_gb - other.disk_gb,
            wall_time_minutes=self.wall_time_minutes,
        )


@dataclass(frozen=True)
class JobSpec:
    """Portable execution request; contains no backend credentials."""

    campaign_id: str
    command: tuple[str, ...]
    resources: ResourceProfile
    environment: Mapping[str, str] = field(default_factory=dict)
    checkpoint_uri: str | None = None
    resume_from: str | None = None
    preemptible: bool = False
    max_retries: int = 0
    timeout_minutes: int | None = None
    artifact_result_path: str | None = None


@dataclass(frozen=True)
class JobState:
    campaign_id: str
    state: str
    attempt: int = 0
    backend_job_id: str | None = None
    checkpoint_uri: str | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if self.state not in KNOWN_STATES:
            raise PortfolioError(f"unknown state {self.state!r} for {self.campaign_id}")
        if self.attempt < 0:
            raise PortfolioError("attempt cannot be negative")


@dataclass(frozen=True)
class ScheduleDecision:
    jobs: tuple[JobSpec, ...]
    blocked: Mapping[str, str]
    available: ResourceProfile


def _detect_cycle(dependencies: Mapping[str, tuple[str, ...]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, path: tuple[str, ...]) -> None:
        if node in visiting:
            start = path.index(node)
            raise PortfolioError("dependency cycle: " + " -> ".join((*path[start:], node)))
        if node in visited:
            return
        visiting.add(node)
        for dependency in dependencies[node]:
            visit(dependency, (*path, node))
        visiting.remove(node)
        visited.add(node)

    for campaign_id in sorted(dependencies):
        visit(campaign_id, ())


def schedule(
    campaigns: Sequence[Mapping[str, Any]],
    states: Mapping[str, JobState | Mapping[str, Any]],
    capacity: ResourceProfile | Mapping[str, Any],
    *,
    max_concurrency: int,
    remaining_cost: float | None = None,
) -> ScheduleDecision:
    """Return the deterministic ready frontier for a portfolio snapshot.

    Lower numeric priority runs first.  Failed or skipped optional branches only
    block their descendants.  Existing running jobs consume capacity and slots.
    ``estimated_cost`` is an admission estimate, not a billing mechanism.
    """
    if max_concurrency < 1:
        raise PortfolioError("max_concurrency must be positive")
    capacity = (capacity if isinstance(capacity, ResourceProfile)
                else ResourceProfile.from_mapping(capacity, allow_zero_cpus=True))
    by_id: dict[str, Mapping[str, Any]] = {}
    for campaign in campaigns:
        campaign_id = str(campaign.get("id", "")).strip()
        if not campaign_id:
            raise PortfolioError("every campaign requires a non-empty id")
        if campaign_id in by_id:
            raise PortfolioError(f"duplicate campaign id: {campaign_id}")
        by_id[campaign_id] = campaign
    dependencies = {cid: tuple(map(str, item.get("depends_on", ()))) for cid, item in by_id.items()}
    missing = sorted({dep for deps in dependencies.values() for dep in deps if dep not in by_id})
    if missing:
        raise PortfolioError("unknown dependencies: " + ", ".join(missing))
    _detect_cycle(dependencies)

    normalized: dict[str, JobState] = {}
    for cid in by_id:
        raw = states.get(cid, {"campaign_id": cid, "state": "planned"})
        normalized[cid] = raw if isinstance(raw, JobState) else JobState(**dict(raw))
        if normalized[cid].campaign_id != cid:
            raise PortfolioError(f"state key {cid!r} does not match campaign_id {normalized[cid].campaign_id!r}")

    available = capacity
    running = 0
    blocked: dict[str, str] = {}
    for cid, state in normalized.items():
        if state.state == "running":
            running += 1
            resources = ResourceProfile.from_mapping(by_id[cid].get("resources"))
            if not resources.fits(available):
                raise PortfolioError(f"running campaign {cid} exceeds declared portfolio capacity")
            available = available.subtract(resources)
    slots = max_concurrency - running
    if slots < 0:
        raise PortfolioError("running jobs exceed max_concurrency")

    candidates: list[tuple[int, str, Mapping[str, Any], ResourceProfile]] = []
    for cid, campaign in by_id.items():
        state = normalized[cid]
        if state.state in TERMINAL_SUCCESS | {"running"}:
            continue
        if state.state == "blocked":
            blocked[cid] = state.message or "externally blocked"
            continue
        max_retries = int(campaign.get("max_retries", 0))
        if state.state == "failed" and state.attempt > max_retries:
            blocked[cid] = "retry budget exhausted"
            continue
        unmet = [dep for dep in dependencies[cid] if normalized[dep].state != "completed"]
        if unmet:
            failed = [dep for dep in unmet if normalized[dep].state in {"failed", "skipped"}]
            blocked[cid] = ("dependency failed/skipped: " if failed else "waiting for dependencies: ") + ", ".join(failed or unmet)
            continue
        resources = ResourceProfile.from_mapping(campaign.get("resources"))
        candidates.append((int(campaign.get("priority", 100)), cid, campaign, resources))

    jobs: list[JobSpec] = []
    cost_left = remaining_cost
    for _, cid, campaign, resources in sorted(candidates, key=lambda item: (item[0], item[1])):
        if len(jobs) >= slots:
            blocked[cid] = "concurrency limit"
            continue
        if not resources.fits(available):
            blocked[cid] = "insufficient resources"
            continue
        cost = float(campaign.get("estimated_cost", 0.0))
        if cost < 0:
            raise PortfolioError(f"estimated_cost cannot be negative for {cid}")
        if cost_left is not None and cost > cost_left:
            blocked[cid] = "cost budget"
            continue
        command = campaign.get("command")
        if not isinstance(command, (list, tuple)) or not command or not all(isinstance(arg, str) for arg in command):
            raise PortfolioError(f"campaign {cid} requires command as a non-empty string sequence")
        jobs.append(JobSpec(
            campaign_id=cid,
            command=tuple(command),
            resources=resources,
            environment=dict(campaign.get("environment", {})),
            checkpoint_uri=campaign.get("checkpoint_uri"),
            resume_from=normalized[cid].checkpoint_uri,
            preemptible=bool(campaign.get("preemptible", False)),
            max_retries=int(campaign.get("max_retries", 0)),
            timeout_minutes=int(campaign.get("timeout_minutes", resources.wall_time_minutes)),
            artifact_result_path=campaign.get("artifact_result_path"),
        ))
        available = available.subtract(resources)
        if cost_left is not None:
            cost_left -= cost
    return ScheduleDecision(tuple(jobs), blocked, available)
