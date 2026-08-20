from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from ._validation import ContractError, exact_keys, integer, text


@dataclass(frozen=True)
class CampaignLease:
    schema_version: int
    lease_id: str
    campaign_id: str
    owner: str
    lane: str
    device: str
    exclusive: bool
    cpu_threads: int
    cotenancy: str
    throughput_gated: bool
    state_root: str
    checkout: str
    cache_root: str
    ledger_path: str
    acquired_at: float
    expires_at: float

    VERSION = 1

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CampaignLease":
        exact_keys(value, set(cls.__dataclass_fields__), "campaign lease")
        version = integer(value.get("schema_version"), "schema_version", minimum=1)
        if version != cls.VERSION:
            raise ContractError(f"unsupported CampaignLease schema_version {version}")
        lane = text(value.get("lane"), "lane")
        if lane not in {"cpu", "gpu"}:
            raise ContractError("lane must be cpu or gpu")
        cotenancy = text(value.get("cotenancy"), "cotenancy")
        if cotenancy not in {"allow", "forbid"}:
            raise ContractError("cotenancy must be allow or forbid")
        flags = (value.get("exclusive"), value.get("throughput_gated"))
        if not all(isinstance(flag, bool) for flag in flags):
            raise ContractError("exclusive and throughput_gated must be boolean")
        acquired, expires = value.get("acquired_at"), value.get("expires_at")
        if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in (acquired, expires)):
            raise ContractError("lease timestamps must be numeric")
        if float(expires) <= float(acquired):
            raise ContractError("expires_at must be after acquired_at")
        strings = [text(value.get(name), name) for name in
                   ("lease_id", "campaign_id", "owner")]
        paths = [text(value.get(name), name) for name in
                 ("device", "state_root", "checkout", "cache_root", "ledger_path")]
        return cls(version, *strings, lane, paths[0], flags[0],
                   integer(value.get("cpu_threads"), "cpu_threads", minimum=1), cotenancy, flags[1],
                   *paths[1:], float(acquired), float(expires))

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeaseSet:
    leases: tuple[CampaignLease, ...]

    def __post_init__(self) -> None:
        for index, left in enumerate(self.leases):
            for right in self.leases[index + 1:]:
                shared = sorted({left.state_root, left.checkout, left.cache_root, left.ledger_path} &
                                {right.state_root, right.checkout, right.cache_root, right.ledger_path})
                if shared:
                    raise ContractError(f"campaign leases share isolation namespace: {shared[0]}")
                if left.device == right.device and (left.exclusive or right.exclusive or
                        left.cotenancy == "forbid" or right.cotenancy == "forbid"):
                    raise ContractError(f"campaign leases conflict on device {left.device}")
