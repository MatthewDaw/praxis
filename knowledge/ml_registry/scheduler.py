"""Deterministic, backend-neutral scheduling for ML campaign portfolios.

The scheduler deliberately does not launch processes or provision machines.  It
turns a portfolio snapshot into a validated ready frontier; an executor can then
translate :class:`JobSpec` to a local, batch, or cloud backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Mapping, Sequence


TERMINAL_SUCCESS = frozenset({"completed", "skipped"})
#: The closed set of campaign-level keys ``schedule`` understands.  A typo such as
#: ``timeout_minute`` must refuse loudly rather than be silently dropped.
CAMPAIGN_KEYS = frozenset({
    "id", "depends_on", "resources", "command", "environment", "checkpoint_uri",
    "preemptible", "max_retries", "priority", "timeout_minutes", "estimated_cost",
    "artifact_result_path", "working_directory",
})
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

    def __post_init__(self) -> None:
        integer_fields = ("cpus", "gpus", "wall_time_minutes")
        numeric_fields = (*integer_fields, "gpu_vram_gb", "ram_gb", "disk_gb")
        for name in numeric_fields:
            item = getattr(self, name)
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise PortfolioError(f"resource {name} must be numeric")
            if not math.isfinite(float(item)):
                raise PortfolioError(f"resource {name} must be finite")
        for name in integer_fields:
            if not isinstance(getattr(self, name), int):
                raise PortfolioError(f"resource {name} must be an integer")
        if any(getattr(self, name) < 0 for name in numeric_fields) or self.wall_time_minutes == 0:
            raise PortfolioError("resource quantities must be non-negative; wall_time_minutes must be positive")
        if self.gpus == 0 and self.gpu_vram_gb:
            raise PortfolioError("gpu_vram_gb requires at least one GPU")

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
        *,
        allow_zero_cpus: bool = False,
    ) -> "ResourceProfile":
        """Parse resources, allowing an empty CPU pool only for capacity snapshots."""
        if value is not None and not isinstance(value, Mapping):
            raise PortfolioError("resources must be an object")
        raw = dict(value or {})
        unknown = sorted(set(raw) - set(cls.__dataclass_fields__))
        if unknown:
            raise PortfolioError("unknown resource fields: " + ", ".join(unknown))
        integer_fields = {"cpus", "gpus", "wall_time_minutes"}
        for name, item in raw.items():
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise PortfolioError(f"resource {name} must be numeric")
            if not math.isfinite(float(item)):
                raise PortfolioError(f"resource {name} must be finite")
            if name in integer_fields and (not isinstance(item, int) or isinstance(item, bool)):
                raise PortfolioError(f"resource {name} must be an integer")
        try:
            profile = cls(**raw)
        except TypeError as exc:
            raise PortfolioError(f"invalid resources: {exc}") from exc
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
    working_directory: str | None = None


@dataclass(frozen=True)
class JobState:
    campaign_id: str
    state: str
    attempt: int = 0
    backend_job_id: str | None = None
    checkpoint_uri: str | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.campaign_id, str) or not self.campaign_id:
            raise PortfolioError("state campaign_id must be a non-empty string")
        if self.state not in KNOWN_STATES:
            raise PortfolioError(f"unknown state {self.state!r} for {self.campaign_id}")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 0:
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
    if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
        raise PortfolioError("max_concurrency must be positive")
    if remaining_cost is not None:
        remaining_cost = _finite(remaining_cost, "remaining_cost")
        if remaining_cost < 0:
            raise PortfolioError("remaining_cost cannot be negative")
    capacity = (capacity if isinstance(capacity, ResourceProfile)
                else ResourceProfile.from_mapping(capacity, allow_zero_cpus=True))
    by_id: dict[str, Mapping[str, Any]] = {}
    for campaign in campaigns:
        if not isinstance(campaign, Mapping):
            raise PortfolioError("every campaign must be an object")
        unknown = sorted(set(campaign) - CAMPAIGN_KEYS)
        if unknown:
            raise PortfolioError("unknown campaign fields: " + ", ".join(unknown))
        campaign_id = campaign.get("id", "")
        if not isinstance(campaign_id, str) or not campaign_id.strip():
            raise PortfolioError("every campaign requires a non-empty id")
        campaign_id = campaign_id.strip()
        if campaign_id in by_id:
            raise PortfolioError(f"duplicate campaign id: {campaign_id}")
        by_id[campaign_id] = campaign
    dependencies: dict[str, tuple[str, ...]] = {}
    for cid, item in by_id.items():
        raw_dependencies = item.get("depends_on", ())
        if (not isinstance(raw_dependencies, (list, tuple))
                or not all(isinstance(dep, str) and dep for dep in raw_dependencies)):
            raise PortfolioError(f"depends_on for {cid} must be a string sequence")
        dependencies[cid] = tuple(raw_dependencies)
    missing = sorted({dep for deps in dependencies.values() for dep in deps if dep not in by_id})
    if missing:
        raise PortfolioError("unknown dependencies: " + ", ".join(missing))
    _detect_cycle(dependencies)

    if not isinstance(states, Mapping):
        raise PortfolioError("states must be an object keyed by campaign id")
    normalized: dict[str, JobState] = {}
    for cid in by_id:
        raw = states.get(cid, {"campaign_id": cid, "state": "planned"})
        if isinstance(raw, JobState):
            normalized[cid] = raw
        elif isinstance(raw, Mapping):
            try:
                normalized[cid] = JobState(**dict(raw))
            except (TypeError, ValueError) as exc:
                if isinstance(exc, PortfolioError):
                    raise
                raise PortfolioError(f"invalid state for {cid}: {exc}") from exc
        else:
            raise PortfolioError(f"state for {cid} must be an object or JobState")
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
        max_retries = _integer(campaign.get("max_retries", 0), f"max_retries for {cid}", minimum=0)
        if state.state == "failed" and state.attempt > max_retries:
            blocked[cid] = "retry budget exhausted"
            continue
        unmet = [dep for dep in dependencies[cid] if normalized[dep].state != "completed"]
        if unmet:
            failed = [dep for dep in unmet if normalized[dep].state in {"failed", "skipped"}]
            blocked[cid] = ("dependency failed/skipped: " if failed else "waiting for dependencies: ") + ", ".join(failed or unmet)
            continue
        resources = ResourceProfile.from_mapping(campaign.get("resources"))
        candidates.append((_integer(campaign.get("priority", 100), f"priority for {cid}"), cid, campaign, resources))

    jobs: list[JobSpec] = []
    cost_left = remaining_cost
    for _, cid, campaign, resources in sorted(candidates, key=lambda item: (item[0], item[1])):
        if len(jobs) >= slots:
            blocked[cid] = "concurrency limit"
            continue
        if not resources.fits(available):
            blocked[cid] = "insufficient resources"
            continue
        cost = _finite(campaign.get("estimated_cost", 0.0), f"estimated_cost for {cid}")
        if cost < 0:
            raise PortfolioError(f"estimated_cost cannot be negative for {cid}")
        if cost_left is not None and cost > cost_left:
            blocked[cid] = "cost budget"
            continue
        command = campaign.get("command")
        if (not isinstance(command, (list, tuple)) or not command
                or not all(isinstance(arg, str) and arg for arg in command)):
            raise PortfolioError(f"campaign {cid} requires command as a non-empty string sequence")
        checkpoint_uri = _optional_string(campaign.get("checkpoint_uri"), f"checkpoint_uri for {cid}")
        artifact_result_path = _optional_string(
            campaign.get("artifact_result_path"), f"artifact_result_path for {cid}"
        )
        working_directory = _optional_string(
            campaign.get("working_directory"), f"working_directory for {cid}"
        )
        jobs.append(JobSpec(
            campaign_id=cid,
            command=tuple(command),
            resources=resources,
            environment=_string_mapping(campaign.get("environment", {}), f"environment for {cid}"),
            checkpoint_uri=checkpoint_uri,
            resume_from=normalized[cid].checkpoint_uri,
            preemptible=_boolean(campaign.get("preemptible", False), f"preemptible for {cid}"),
            max_retries=_integer(campaign.get("max_retries", 0), f"max_retries for {cid}", minimum=0),
            timeout_minutes=_integer(campaign.get("timeout_minutes", resources.wall_time_minutes),
                                     f"timeout_minutes for {cid}", minimum=1),
            artifact_result_path=artifact_result_path,
            working_directory=working_directory,
        ))
        available = available.subtract(resources)
        if cost_left is not None:
            cost_left -= cost
    return ScheduleDecision(tuple(jobs), blocked, available)


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PortfolioError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise PortfolioError(f"{label} must be finite")
    return result


def _integer(value: object, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PortfolioError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise PortfolioError(f"{label} must be at least {minimum}")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise PortfolioError(f"{label} must be boolean")
    return value


def _string_mapping(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise PortfolioError(f"{label} must be an object of strings")
    return dict(value)


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise PortfolioError(f"{label} must be a non-empty string or null")
    return value
