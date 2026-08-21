from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from ._validation import ContractError, exact_keys, integer, text


@dataclass(frozen=True)
class CodeRef:
    """Immutable git provenance for one run; code itself never enters the registry."""

    schema_version: int
    repo: str
    sha: str
    base_sha: str
    diff_hash: str
    diff_lines: int

    VERSION = 1

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CodeRef":
        exact_keys(value, set(cls.__dataclass_fields__), "code_ref")
        values = [text(value.get(name), name) for name in ("repo", "sha", "base_sha", "diff_hash")]
        for name, value_ in zip(("sha", "base_sha", "diff_hash"), values[1:], strict=True):
            if len(value_) not in {40, 64} or any(ch not in "0123456789abcdef" for ch in value_.lower()):
                raise ContractError(f"{name} must be a full 40- or 64-character hexadecimal digest")
        version = integer(value.get("schema_version"), "schema_version", minimum=1)
        if version != cls.VERSION:
            raise ContractError(f"unsupported CodeRef schema_version {version}")
        return cls(version, values[0], values[1].lower(), values[2].lower(), values[3].lower(),
                   integer(value.get("diff_lines"), "diff_lines", minimum=0))

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LegacyCodeRef:
    """Honest provenance for an archive label that cannot identify a git commit."""

    schema_version: int
    source_ref: str
    provenance_status: str

    VERSION = 1

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LegacyCodeRef":
        exact_keys(value, set(cls.__dataclass_fields__), "legacy_code_ref")
        status = text(value.get("provenance_status"), "provenance_status")
        if status not in {"abbreviated", "unknown"}:
            raise ContractError("legacy provenance_status must be abbreviated or unknown")
        version = integer(value.get("schema_version"), "schema_version", minimum=1)
        if version != cls.VERSION:
            raise ContractError(f"unsupported LegacyCodeRef schema_version {version}")
        return cls(version, text(value.get("source_ref"), "source_ref"), status)

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)
