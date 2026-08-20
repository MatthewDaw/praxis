from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from ._validation import ContractError, exact_keys, integer, text


@dataclass(frozen=True)
class LaunchIntent:
    schema_version: int
    intent_id: str
    campaign_id: str
    attempt: int
    spec_digest: str
    lease_ids: tuple[str, ...]
    registry_trial_id: str | None
    state: str
    created_at: float
    pid: int | None = None
    pgid: int | None = None

    VERSION = 1

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LaunchIntent":
        exact_keys(value, set(cls.__dataclass_fields__), "launch intent")
        version = integer(value.get("schema_version"), "schema_version", minimum=1)
        if version != cls.VERSION:
            raise ContractError(f"unsupported LaunchIntent schema_version {version}")
        state = text(value.get("state"), "state")
        if state not in {"prepared", "claimed", "spawned", "terminal"}:
            raise ContractError(f"unknown launch intent state {state!r}")
        leases = value.get("lease_ids")
        if not isinstance(leases, (list, tuple)) or not leases or not all(isinstance(x, str) and x for x in leases):
            raise ContractError("lease_ids must be a non-empty string sequence")
        digest = text(value.get("spec_digest"), "spec_digest").lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ContractError("spec_digest must be 64 lowercase hexadecimal characters")
        trial = value.get("registry_trial_id")
        if trial is not None:
            trial = text(trial, "registry_trial_id")
        ids = []
        for name in ("pid", "pgid"):
            item = value.get(name)
            ids.append(None if item is None else integer(item, name, minimum=1))
        created = value.get("created_at")
        if isinstance(created, bool) or not isinstance(created, (int, float)):
            raise ContractError("created_at must be numeric")
        if state in {"spawned", "terminal"} and (ids[0] is None or ids[1] is None):
            raise ContractError(f"{state} launch intent requires pid and pgid")
        return cls(version, text(value.get("intent_id"), "intent_id"),
                   text(value.get("campaign_id"), "campaign_id"),
                   integer(value.get("attempt"), "attempt", minimum=1), digest, tuple(leases), trial,
                   state, float(created), ids[0], ids[1])

    def to_mapping(self) -> dict[str, Any]:
        value = asdict(self)
        value["lease_ids"] = list(self.lease_ids)
        return value
