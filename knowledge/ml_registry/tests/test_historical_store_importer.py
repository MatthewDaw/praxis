from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from knowledge.ml_registry import HistoricalStoreImporter, Registry, RunsExport


LEDGER = (
    b"commit\tmetric_value\tmemory_gb\tstatus\tdescription\tthroughput\tdiff_lines\r\n"
    b"abc1234:arm\t.8\t1\tok\tarm\t2\t3\r\n"
)


def _archive(root: Path, *, identity: bool = True) -> Path:
    root.mkdir()
    files = {
        "ledger.tsv": LEDGER,
        "dispositions.json": json.dumps({
            "ledger_rows": [{"commit": "abc1234:arm", "disposition": "incomplete", "reason": "unknown"}],
        }).encode(),
        "registry/model_meta.json": json.dumps({
            **({"experiment_id": "experiment", "model_id": "model"} if identity else {}),
            "metric": "score", "direction": "maximize",
        }, sort_keys=True).encode(),
        "promotion.json": b'{"promoted":[],"rejected":[],"unresolved":["unknown"]}',
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    manifest = {
        "archive_format_version": 1,
        "files": [{"path": path, "bytes": len(content),
                   "sha256": hashlib.sha256(content).hexdigest()}
                  for path, content in files.items()],
    }
    (root / "MANIFEST.json").write_text(json.dumps(manifest, sort_keys=True))
    return root


def test_archive_import_is_atomic_idempotent_and_byte_exact(tmp_path: Path) -> None:
    source = _archive(tmp_path / "archive")
    registry = Registry(tmp_path / "registry")
    importer = HistoricalStoreImporter(registry, archive_root=tmp_path)

    first = importer.import_archive(source)
    digest = registry.snapshot_digest()
    second = importer.import_archive(source)

    assert first == second.__class__(first.import_id, first.experiment_id, 0)
    assert registry.snapshot_digest() == digest
    assert RunsExport.from_registry(registry, import_id=first.import_id).serialize() == LEDGER
    assert registry.model_versions() == []
    assert registry.aliases() == []
    assert registry.verify_event_chain()


def test_identity_and_hash_refusals_write_no_projection(tmp_path: Path) -> None:
    source = _archive(tmp_path / "archive", identity=False)
    registry = Registry(tmp_path / "registry")
    importer = HistoricalStoreImporter(registry)

    with pytest.raises(ValueError, match="experiment_id"):
        importer.import_archive(source)
    assert registry.is_empty()

    with pytest.raises(ValueError, match="hash"):
        importer.import_archive(
            source,
            mappings={"experiment_id": "explicit", "model_id": "explicit-model"},
            source_overrides={"ledger.tsv": b"tampered\n"},
        )
    assert registry.is_empty()


def test_late_projection_refusal_leaves_no_new_cas_blobs(tmp_path: Path) -> None:
    source = _archive(tmp_path / "archive")
    registry = Registry(tmp_path / "registry")
    importer = HistoricalStoreImporter(registry)
    importer.import_archive(source)
    before = {path.relative_to(registry.blobs.root) for path in registry.blobs.root.rglob("*") if path.is_file()}

    second = _archive(tmp_path / "second")
    extra = second / "new-evidence.bin"
    extra.write_bytes(b"new evidence whose projection will collide")
    manifest = json.loads((second / "MANIFEST.json").read_text())
    manifest["files"].append({
        "path": "new-evidence.bin", "bytes": extra.stat().st_size,
        "sha256": hashlib.sha256(extra.read_bytes()).hexdigest(),
    })
    (second / "MANIFEST.json").write_text(json.dumps(manifest, sort_keys=True))

    with pytest.raises(Exception, match="UNIQUE|unique"):
        importer.import_archive(second)
    after = {path.relative_to(registry.blobs.root) for path in registry.blobs.root.rglob("*") if path.is_file()}
    assert after == before

def test_live_freeze_is_evidence_only_and_cannot_shadow(tmp_path: Path) -> None:
    source = _archive(tmp_path / "archive")
    registry = Registry(tmp_path / "registry")
    importer = HistoricalStoreImporter(registry)
    importer.import_archive(source)
    canonical = registry.canonical_projection_digest()

    freeze = tmp_path / "freeze"
    freeze.mkdir()
    content = b"unadjudicated"
    (freeze / "state.bin").write_bytes(content)
    manifest = {
        "archive_format_version": 1,
        "metadata": {"canonical_campaign_archive": False, "adjudication": "none; evidence only"},
        "files": [{"path": "state.bin", "bytes": len(content),
                   "sha256": hashlib.sha256(content).hexdigest()}],
    }
    (freeze / "MANIFEST.json").write_text(json.dumps(manifest))
    importer.import_evidence_freeze(freeze)

    assert registry.canonical_projection_digest() == canonical
    assert registry.verdicts() == []
    assert registry.aliases() == []


def _sports_archive_root() -> Path:
    import subprocess

    common = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    praxis = Path(common).resolve().parent
    return praxis.parent / "sports_analysis" / "ml" / "archive"


@pytest.mark.parametrize(
    ("name", "mappings", "incomplete", "voided"),
    (
        ("association", {}, 10, 0),
        ("ball_campaign", {"experiment_id": "sports_analysis-perception-ball-campaign",
                           "model_id": "model-2677204cd5d6"}, 29, 0),
        ("contact_point", {}, 12, 0),
        ("court_marking", {"experiment_id": "sports_analysis-perception-court-marking",
                           "model_id": "court-marking-seg"}, 4, 0),
        ("detection", {}, 42, 0),
        ("detection_shipped", {}, 26, 11),
        ("stroke", {"experiment_id": "sports_analysis-tennis-stroke",
                    "model_id": "model-8b7e4a6406eb"}, 37, 0),
    ),
)
def test_real_sealed_archives_preserve_dispositions_without_promotion(
    tmp_path: Path, name: str, mappings: dict[str, str], incomplete: int, voided: int,
) -> None:
    archive = _sports_archive_root() / name / "20260820T2024Z-3d7a8a5353d4"
    if not archive.is_dir():
        pytest.skip("sports_analysis archive checkout is unavailable")
    registry = Registry(tmp_path / name)
    HistoricalStoreImporter(registry, archive_root=_sports_archive_root()).import_archive(
        archive, mappings=mappings,
    )
    runs = registry.rows("runs")
    assert sum(row["status"] == "complete" and row["verdict"] is None for row in runs) == incomplete
    assert sum(row["status"] == "voided" and row["verdict"] == "voided" for row in runs) == voided
    assert registry.model_versions() == []
    assert registry.aliases() == []
    assert all(json.loads(row["metrics"])["export_status"] == "ok" for row in runs if row["verdict"] == "voided")
    validity = [json.loads(row["metrics"])["validity"] for row in runs]
    if name == "detection_shipped":
        assert validity.count("invalid") == 11
        assert validity.count("unknown") == 26
    else:
        assert validity == ["unknown"] * incomplete


def test_real_ambiguous_archive_identities_require_explicit_mappings(tmp_path: Path) -> None:
    root = _sports_archive_root()
    if not root.is_dir():
        pytest.skip("sports_analysis archive checkout is unavailable")
    importer = HistoricalStoreImporter(Registry(tmp_path / "registry"), archive_root=root)
    for name, key, mappings in (
        ("ball_campaign", "experiment_id", {"model_id": "model-2677204cd5d6"}),
        ("stroke", "experiment_id", {"model_id": "model-8b7e4a6406eb"}),
        ("court_marking", "model_id", {"experiment_id": "sports_analysis-perception-court-marking"}),
    ):
        with pytest.raises(ValueError, match=key):
            importer.import_archive(root / name / "20260820T2024Z-3d7a8a5353d4", mappings=mappings)
