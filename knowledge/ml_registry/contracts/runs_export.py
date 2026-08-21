from __future__ import annotations

import csv
import io
import json
from typing import TYPE_CHECKING

from ._validation import ContractError
from .ledger_v2 import LEDGER_V2_HEADER, LedgerV2

if TYPE_CHECKING:
    from knowledge.ml_registry.storage.registry import Registry


class RunsExport:
    """Legacy LedgerV2 view derived from canonical ``runs`` rows."""

    def __init__(self, registry: "Registry" | None = None, *, content: str | None = None) -> None:
        self.registry = registry
        self.content = content

    @classmethod
    def from_registry(cls, registry: "Registry", experiment_id: str) -> "RunsExport":
        rows = [row for row in registry.rows("runs") if row["experiment_id"] == experiment_id]
        rows.sort(key=lambda row: (row["started_at"], row["run_id"]))
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(LEDGER_V2_HEADER)
        for row in rows:
            params = json.loads(row["params"])
            metrics = json.loads(row["metrics"])
            preserved = params.get("_runs_export_fields")
            if preserved is not None:
                if not isinstance(preserved, list) or len(preserved) != len(LEDGER_V2_HEADER):
                    raise ContractError("run has malformed _runs_export_fields")
                writer.writerow(preserved)
                continue
            code_ref = json.loads(row["code_ref"])
            status = metrics.get("export_status")
            if status is None:
                status = "ok" if row["status"] in {"complete", "succeeded", "failed"} else row["status"]
            writer.writerow((code_ref["sha"], metrics["metric"], metrics.get("memory_gb", 0), status,
                             params.get("description", row["idea_id"]), metrics.get("throughput", 0),
                             code_ref["diff_lines"]))
        content = stream.getvalue()
        LedgerV2.parse(content)
        return cls(content=content)

    def render(self, *, experiment_id: str) -> bytes:
        if self.registry is None:
            raise ContractError("RunsExport.render requires a registry")
        return self.from_registry(self.registry, experiment_id).serialize().encode()

    def serialize(self) -> str:
        if self.content is None:
            raise ContractError("RunsExport has no rendered content")
        return self.content
