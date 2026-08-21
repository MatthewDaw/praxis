from __future__ import annotations

import csv
import hashlib
import io

from knowledge.ml_registry.contracts import LEDGER_V2_HEADER, LedgerAnnotations, LedgerRowIdentity, LedgerValidity
from knowledge.ml_registry.storage.registry import Registry


class HistoricalLedgerImportError(ValueError):
    pass


class HistoricalLedgerImporter:
    """Import frozen ledger bytes without inventing missing git or lifecycle provenance."""

    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    def import_ledger(
        self, content: str | bytes, *, experiment_id: str, spec_digest: str, metric: str,
        direction: str, repo: str | None = None, source_path: str = "ledger.tsv",
        archive_manifest_hash: str = "unknown", annotations: LedgerAnnotations | None = None,
    ) -> int:
        del repo  # Historical labels are evidence, never upgraded to git provenance implicitly.
        source_bytes = content.encode() if isinstance(content, str) else bytes(content)
        try:
            decoded = source_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HistoricalLedgerImportError("historical ledger must be UTF-8") from exc
        reader = csv.reader(io.StringIO(decoded, newline=""), delimiter="\t")
        try:
            header = tuple(next(reader))
        except StopIteration as exc:
            raise HistoricalLedgerImportError("historical ledger is empty") from exc
        if header != LEDGER_V2_HEADER:
            raise HistoricalLedgerImportError("historical ledger must have the LedgerV2 header")
        raw_rows = [fields for fields in reader if fields]
        source_digest, _ = self.registry.blobs.put(source_bytes)
        import_id = hashlib.sha256(
            (archive_manifest_hash + "\0" + source_path + "\0" + source_digest).encode()
        ).hexdigest()
        experiment = {
            "experiment_id": experiment_id, "spec_digest": spec_digest, "stages": ["legacy"],
            "metric": metric, "direction": direction, "win_condition": {"legacy": "unknown"},
            "noise_floor": 0, "baseline_throughput": 0,
        }
        occurrences: dict[str, int] = {}
        runs: list[dict[str, object]] = []
        for index, fields in enumerate(raw_rows, 1):
            if len(fields) != len(header):
                raise HistoricalLedgerImportError(f"historical ledger line {index + 1} has wrong width")
            commit = fields[0]
            occurrence = occurrences.get(commit, 0)
            occurrences[commit] = occurrence + 1
            validity = annotations.validity.get(LedgerRowIdentity(commit, occurrence)) if annotations else None
            voided = validity == LedgerValidity.INVALID or fields[3] != "ok"
            row_digest = hashlib.sha256(
                (archive_manifest_hash + "\0" + source_path + "\0" + str(index) + "\0" + "\t".join(fields)).encode()
            ).hexdigest()
            runs.append({
                "run_id": f"legacy-{row_digest[:24]}", "experiment_id": experiment_id,
                "idea_id": fields[4], "stage": "legacy", "family": "legacy",
                "params": {"description": fields[4], "import_provenance": {
                    "source_blob_sha256": source_digest, "source_path": source_path,
                    "archive_manifest_hash": archive_manifest_hash, "row_ordinal": index,
                }},
                "metrics": {"metric": float(fields[1]), "memory_gb": float(fields[2]),
                            "throughput": float(fields[5]), "export_status": fields[3],
                            "validity": (validity.value if isinstance(validity, LedgerValidity)
                                         else validity or "unknown")},
                "code_ref": {"schema_version": 1, "source_ref": commit,
                             "provenance_status": "abbreviated" if commit else "unknown"},
                "device_fingerprint": "legacy:unknown",
                "status": "voided" if voided else "complete",
                "verdict": "voided" if voided else None,
                "started_at": float(index), "finished_at": float(index),
                "claim_owner": "historical-import", "heartbeat_at": float(index),
            })
        self.registry.import_historical_ledger(import_id=import_id, experiment=experiment, runs=runs,
                                               source_blob_sha256=source_digest)
        return len(raw_rows)
