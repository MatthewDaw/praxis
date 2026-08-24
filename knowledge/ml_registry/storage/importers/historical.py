from __future__ import annotations

import csv
from dataclasses import dataclass, field
import hashlib
import io
import json
from pathlib import Path
from typing import Any, Mapping

from knowledge.ml_registry.contracts import LEDGER_V2_HEADER
from knowledge.ml_registry.storage.registry import Registry


@dataclass(frozen=True)
class HistoricalImportResult:
    import_id: str
    experiment_id: str | None
    inserted: int = field(compare=False)


class HistoricalStoreImporter:
    """Losslessly project sealed pre-registry stores into an evidence-only history.

    Source paths are opened read-only.  Identity is accepted only from unambiguous
    structured archive evidence or from an explicit caller mapping.
    """

    def __init__(self, registry: Registry, *, archive_root: str | Path | None = None) -> None:
        self.registry = registry
        self.archive_root = Path(archive_root).resolve() if archive_root is not None else None

    def import_archive(
        self, archive: str | Path, *, mappings: Mapping[str, str] | None = None,
        source_overrides: Mapping[str, bytes] | None = None,
    ) -> HistoricalImportResult:
        root = self._source(archive)
        supplied = dict(mappings or {})
        overrides = dict(source_overrides or {})
        manifest_bytes = self._read(root, "MANIFEST.json", overrides)
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("archive manifest is not valid UTF-8 JSON") from exc
        files = self._verify_manifest(root, manifest, overrides)
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()

        experiment_id = self._identity(root, files, "experiment_id", supplied)
        model_id = self._identity(root, files, "model_id", supplied, required=False)
        ledger_path = self._ledger_path(files)
        ledger = files[ledger_path]
        dispositions = self._dispositions(files)
        rows = self._runs(ledger, manifest_hash, ledger_path, experiment_id, dispositions)
        meta = self._model_meta(root, files)
        experiment = {
            "experiment_id": experiment_id,
            "spec_digest": manifest_hash,
            "stages": meta.get("stages") or ["legacy"],
            "metric": meta.get("metric") or "metric_value",
            "direction": meta.get("direction") if meta.get("direction") in {"maximize", "minimize"} else "maximize",
            "win_condition": meta.get("win_condition") or {"historical": "unasserted"},
            "rope": float(meta.get("rope") or 0),
            "baseline_throughput": float(meta.get("baseline_throughput") or 0),
        }
        model = None if model_id is None else {
            "model_id": model_id,
            "family": "historical:unasserted",
            "sport_scope": "historical:unasserted",
            "axis": "historical:unasserted",
            "protocol": "historical-evidence-only",
        }
        evidence_bytes = {"MANIFEST.json": manifest_bytes, **files}
        evidence = [
            {"source_path": path, "blob_sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
            for path, content in sorted(evidence_bytes.items())
        ]
        import_id = hashlib.sha256(("archive\0" + manifest_hash).encode()).hexdigest()
        payload: dict[str, Any] = {
            "import_id": import_id, "manifest_sha256": manifest_hash,
            "experiment": experiment, "runs": rows, "model": model, "evidence": evidence,
        }
        changed = self._commit_with_blobs(payload, evidence_bytes)
        inserted = (1 + len(rows) + len(evidence) + (model is not None)) if changed else 0
        return HistoricalImportResult(import_id, experiment_id, int(inserted))

    def import_evidence_freeze(self, freeze: str | Path) -> HistoricalImportResult:
        root = self._source(freeze)
        manifest_bytes = self._read(root, "MANIFEST.json", {})
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("freeze manifest is not valid UTF-8 JSON") from exc
        metadata = manifest.get("metadata", {})
        if metadata.get("canonical_campaign_archive") is not False or not str(metadata.get("adjudication", "")).startswith("none;"):
            raise ValueError("live freeze must declare noncanonical, unadjudicated evidence")
        files = self._verify_manifest(root, manifest, {})
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        evidence_bytes = {"MANIFEST.json": manifest_bytes, **files}
        evidence = [
            {"source_path": path, "blob_sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
            for path, content in sorted(evidence_bytes.items())
        ]
        import_id = hashlib.sha256(("live-freeze\0" + manifest_hash).encode()).hexdigest()
        payload = {
            "import_id": import_id, "manifest_sha256": manifest_hash, "evidence": evidence,
        }
        changed = self._commit_with_blobs(payload, evidence_bytes, freeze=True)
        return HistoricalImportResult(import_id, None, len(evidence) if changed else 0)

    def _commit_with_blobs(self, payload: Mapping[str, Any], contents: Mapping[str, bytes],
                           *, freeze: bool = False) -> bool:
        """Publish CAS bytes, rolling back only blobs created by this refused import."""
        new_paths: list[Path] = []
        try:
            for content in contents.values():
                digest = hashlib.sha256(content).hexdigest()
                target = self.registry.blobs.path(digest)
                existed = target.exists()
                _, path = self.registry.blobs.put(content)
                if not existed:
                    new_paths.append(path)
            if freeze:
                return self.registry.import_historical_evidence_freeze(payload)
            return self.registry.import_historical_archive(payload)
        except BaseException:
            referenced: set[str] = set()

            def collect(value: object) -> None:
                if isinstance(value, dict):
                    digest = value.get("blob_sha256")
                    if isinstance(digest, str):
                        referenced.add(digest)
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)

            for event in self.registry.events.read():
                collect(event.payload)
            for path in new_paths:
                digest = path.parent.name + path.name
                if digest not in referenced:
                    path.unlink(missing_ok=True)
                    try:
                        path.parent.rmdir()
                    except OSError:
                        pass
            raise

    def _source(self, source: str | Path) -> Path:
        root = Path(source).resolve()
        if self.archive_root is not None and root != self.archive_root and self.archive_root not in root.parents:
            raise ValueError("archive path is outside the configured archive root")
        if not root.is_dir():
            raise ValueError("archive path is not a directory")
        return root

    @staticmethod
    def _read(root: Path, relative: str, overrides: Mapping[str, bytes]) -> bytes:
        if relative in overrides:
            return bytes(overrides[relative])
        path = (root / relative).resolve()
        if root != path and root not in path.parents:
            raise ValueError("archive manifest path escapes its root")
        return path.read_bytes()

    def _verify_manifest(self, root: Path, manifest: object,
                         overrides: Mapping[str, bytes]) -> dict[str, bytes]:
        if not isinstance(manifest, dict) or manifest.get("archive_format_version") != 1:
            raise ValueError("unsupported archive manifest")
        entries = manifest.get("files")
        if not isinstance(entries, list):
            raise ValueError("archive manifest files must be a list")
        expected_paths = {item.get("path") for item in entries if isinstance(item, dict)}
        if set(overrides) - ({"MANIFEST.json"} | expected_paths):
            raise ValueError("source override is absent from archive manifest")
        result: dict[str, bytes] = {}
        for item in entries:
            if not isinstance(item, dict) or set(item) != {"path", "sha256", "bytes"}:
                raise ValueError("invalid archive manifest file entry")
            relative = item["path"]
            if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError("archive manifest contains an unsafe path")
            content = self._read(root, relative, overrides)
            if len(content) != item["bytes"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
                raise ValueError(f"archive manifest hash mismatch for {relative}")
            result[relative] = content
        return result

    @staticmethod
    def _json_documents(files: Mapping[str, bytes]):
        for path, content in files.items():
            if path.endswith(".json"):
                try:
                    yield path, json.loads(content)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue

    def _identity(self, root: Path, files: Mapping[str, bytes], key: str,
                  supplied: Mapping[str, str], *, required: bool = True) -> str | None:
        explicit = supplied.get(key)
        values: set[str] = set()

        def visit(value: object) -> None:
            if isinstance(value, dict):
                found = value.get(key)
                if isinstance(found, str) and found.strip():
                    values.add(found)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        for _, document in self._json_documents(files):
            visit(document)
        if explicit is not None:
            if not isinstance(explicit, str) or not explicit.strip():
                raise ValueError(f"{key} mapping must be a non-empty string")
            return explicit
        if len(values) == 1:
            return next(iter(values))
        if required or values:
            detail = "missing" if not values else "ambiguous"
            raise ValueError(f"{key} is {detail}; provide an explicit mapping for {root.name}")
        return None

    @staticmethod
    def _ledger_path(files: Mapping[str, bytes]) -> str:
        for candidate in ("ledger.tsv", "ledger.baseline.tsv"):
            if candidate in files:
                return candidate
        raise ValueError("archive manifest contains no canonical ledger")

    def _model_meta(self, root: Path, files: Mapping[str, bytes]) -> Mapping[str, Any]:
        del root
        for path in ("registry/model_meta.json", "registry/source/model_meta.json"):
            content = files.get(path)
            if content is not None:
                value = json.loads(content)
                if isinstance(value, dict):
                    return value
        return {}

    @staticmethod
    def _dispositions(files: Mapping[str, bytes]) -> tuple[Mapping[str, Any], ...]:
        content = files.get("dispositions.json")
        if content is None:
            raise ValueError("archive manifest contains no dispositions.json")
        try:
            document = json.loads(content)
            rows = document["ledger_rows"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError("archive dispositions are invalid") from exc
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("archive ledger dispositions must be a list of objects")
        allowed = {"incomplete", "voided"}
        if any(row.get("disposition") not in allowed or not isinstance(row.get("commit"), str) for row in rows):
            raise ValueError("archive ledger disposition is unsupported")
        return tuple(rows)

    @staticmethod
    def _runs(content: bytes, manifest_hash: str, source_path: str,
              experiment_id: str, dispositions: tuple[Mapping[str, Any], ...]) -> list[dict[str, Any]]:
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("historical ledger must be UTF-8") from exc
        reader = csv.reader(io.StringIO(decoded, newline=""), delimiter="\t")
        try:
            header = tuple(next(reader))
        except StopIteration as exc:
            raise ValueError("historical ledger is empty") from exc
        if header != LEDGER_V2_HEADER:
            raise ValueError("historical ledger must have the LedgerV2 header")
        source_digest = hashlib.sha256(content).hexdigest()
        raw_rows = [row for row in reader if row]
        if len(raw_rows) != len(dispositions):
            raise ValueError("archive dispositions do not cover every ledger row exactly once")
        rows = []
        for ordinal, (fields, disposition) in enumerate(zip(raw_rows, dispositions, strict=True), 1):
            if len(fields) != len(header):
                raise ValueError(f"historical ledger line {ordinal + 1} has wrong width")
            row_digest = hashlib.sha256(
                (manifest_hash + "\0" + source_path + "\0" + str(ordinal) + "\0" + "\t".join(fields)).encode()
            ).hexdigest()
            try:
                metric, memory, throughput, diff_lines = float(fields[1]), float(fields[2]), float(fields[5]), int(fields[6])
            except ValueError as exc:
                raise ValueError(f"historical ledger line {ordinal + 1} has invalid numeric data") from exc
            if disposition["commit"] != fields[0]:
                raise ValueError(f"archive disposition identity mismatch at ledger row {ordinal}")
            voided = disposition["disposition"] == "voided"
            rows.append({
                "run_id": f"legacy-{row_digest[:24]}", "experiment_id": experiment_id,
                "idea_id": fields[4], "stage": "legacy", "family": "legacy",
                "params": {"description": fields[4], "import_provenance": {
                    "source_blob_sha256": source_digest, "source_path": source_path,
                    "archive_manifest_hash": manifest_hash, "row_ordinal": ordinal,
                }},
                "metrics": {"metric": metric, "memory_gb": memory, "throughput": throughput,
                            "diff_lines": diff_lines, "export_status": fields[3],
                            "validity": "invalid" if voided else "unknown"},
                "code_ref": {"schema_version": 1, "source_ref": fields[0],
                             "provenance_status": "abbreviated" if fields[0] else "unknown"},
                "device_fingerprint": "legacy:unknown",
                "status": "voided" if voided else "complete", "verdict": "voided" if voided else None,
                "started_at": float(ordinal), "finished_at": float(ordinal),
                "claim_owner": "historical-import", "heartbeat_at": float(ordinal),
            })
        return rows
