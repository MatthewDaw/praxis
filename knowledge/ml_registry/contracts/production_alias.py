from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from ._validation import ContractError, exact_keys, integer, text


@dataclass(frozen=True)
class ProductionAliasRef:
    model_id: str
    version: int
    alias: str = "production"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProductionAliasRef":
        exact_keys(value, set(cls.__dataclass_fields__), "production alias reference")
        alias = text(value.get("alias"), "alias")
        if alias != "production":
            raise ContractError("completed campaign outcome requires the production alias")
        return cls(text(value.get("model_id"), "model_id"),
                   integer(value.get("version"), "version", minimum=1), alias)

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)
