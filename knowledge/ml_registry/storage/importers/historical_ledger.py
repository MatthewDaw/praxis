from __future__ import annotations

import csv
import hashlib
import io
from knowledge.ml_registry.contracts import LEDGER_V2_HEADER, LedgerAnnotations, LedgerRowIdentity, LedgerValidity
from knowledge.ml_registry.storage.registry import Registry


class HistoricalLedgerImportError(ValueError):
    pass


class HistoricalLedgerImporter:
    """Offline importer; it receives frozen bytes and never opens a live campaign path."""

    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    def import_ledger(
        self,
        content: str,
        *,
        experiment_id: str,
        spec_digest: str,
        metric: str,
        direction: str,
        repo: str,
        annotations: LedgerAnnotations | None = None,
    ) -> int:
        reader = csv.reader(io.StringIO(content), delimiter="\t")
        try:
            header = tuple(next(reader))
        except StopIteration as exc:
            raise HistoricalLedgerImportError("historical ledger is empty") from exc
        if header != LEDGER_V2_HEADER:
            raise HistoricalLedgerImportError("historical ledger must have the LedgerV2 header")
        raw_rows = [fields for fields in reader if fields]
        self.registry.create_experiment(
            experiment_id=experiment_id, spec_digest=spec_digest, stages=["legacy"], metric=metric,
            direction=direction, win_condition={"legacy": "unknown"}, noise_floor=0,
            baseline_throughput=0,
        )
        occurrences: dict[str, int] = {}
        for index, fields in enumerate(raw_rows, 1):
            if len(fields) != len(header):
                raise HistoricalLedgerImportError(f"historical ledger line {index + 1} has wrong width")
            commit = fields[0]
            sha = commit.split(":", 1)[0]
            if len(sha) not in {40, 64} or any(ch not in "0123456789abcdef" for ch in sha.lower()):
                raise HistoricalLedgerImportError(f"historical ledger line {index + 1} has no full code sha")
            occurrence = occurrences.get(commit, 0)
            occurrences[commit] = occurrence + 1
            identity = LedgerRowIdentity(commit, occurrence)
            validity = annotations.validity.get(identity) if annotations else None
            if fields[3] == "ok" and float(fields[1]) == 0 and validity is None:
                raise HistoricalLedgerImportError(
                    f"historical zero-metric ok row {commit!r} requires an external validity disposition"
                )
            run_status = "voided" if validity == LedgerValidity.INVALID else (
                "complete" if fields[3] == "ok" else "voided"
            )
            digest = hashlib.sha256((experiment_id + "\0" + str(index) + "\0" + "\t".join(fields)).encode()).hexdigest()
            self.registry.create_run(
                run_id=f"legacy-{digest[:24]}", experiment_id=experiment_id,
                idea_id=fields[4], stage="legacy", family="legacy",
                params={"description": fields[4], "_runs_export_fields": fields},
                metrics={"metric": float(fields[1]), "memory_gb": float(fields[2]),
                         "throughput": float(fields[5]), "export_status": fields[3],
                         "validity": validity.value if isinstance(validity, LedgerValidity) else validity},
                code_ref={"repo": repo, "sha": sha, "base_sha": sha,
                          "diff_hash": "0" * len(sha), "diff_lines": int(fields[6])},
                device_fingerprint="legacy:unknown", status=run_status, verdict=None,
                started_at=float(index), finished_at=float(index), claim_owner="historical-import",
                heartbeat_at=float(index),
            )
        return len(raw_rows)
