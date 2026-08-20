from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from threading import Barrier

import pytest

from knowledge.ml_registry.contracts import CampaignArtifact
from knowledge.ml_registry.storage.artifact_store import ArtifactStore, ArtifactStoreError


def _artifact(source: Path, artifact_id: str = "fit-1", **changes: object) -> CampaignArtifact:
    content = source.read_bytes()
    values: dict[str, object] = {
        "schema_version": 1,
        "artifact_id": artifact_id,
        "artifact_type": "weights",
        "uri": source.resolve().as_uri(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "producer_campaign_id": "campaign-1",
        "trial_id": "trial-1",
        "lineage_id": "lineage-1",
        "interface_version": "v1",
    }
    values.update(changes)
    return CampaignArtifact.from_mapping(values)


def test_ingest_copies_content_addressed_blob_and_replay_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "model.bin"
    source.write_bytes(b"weights")
    store = ArtifactStore(tmp_path / "store", clock=lambda: 10.0)

    first = store.ingest_artifact(source, _artifact(source))
    second = store.ingest_artifact(source, _artifact(source))
    snapshot = store.replay()

    assert first == second == store.verify_artifact("fit-1")
    assert Path(first.uri.removeprefix("file://")).read_bytes() == b"weights"
    assert len(snapshot.events) == 1
    assert snapshot.events[0].occurred_at == 10.0
    assert snapshot.artifacts == {"fit-1": first}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"sha256": "0" * 64}, "sha256 mismatch"),
        ({"size_bytes": 999}, "size mismatch"),
    ],
)
def test_ingest_refuses_source_checksum_or_size_mismatch(
    tmp_path: Path, changes: dict[str, object], message: str,
) -> None:
    source = tmp_path / "model.bin"
    source.write_bytes(b"weights")

    with pytest.raises(ArtifactStoreError, match=message):
        ArtifactStore(tmp_path / "store").ingest_artifact(source, _artifact(source, **changes))
    assert not list((tmp_path / "store" / "events").glob("*.json"))


def test_artifact_id_drift_is_refused_without_appending_history(tmp_path: Path) -> None:
    first_source = tmp_path / "first.bin"
    second_source = tmp_path / "second.bin"
    first_source.write_bytes(b"first")
    second_source.write_bytes(b"second")
    store = ArtifactStore(tmp_path / "store")
    store.ingest_artifact(first_source, _artifact(first_source))

    with pytest.raises(ArtifactStoreError, match="immutable.*drifted"):
        store.ingest_artifact(second_source, _artifact(second_source))

    assert len(store.replay().events) == 1


def test_replay_detects_event_payload_and_hash_chain_tampering(tmp_path: Path) -> None:
    source = tmp_path / "model.bin"
    source.write_bytes(b"weights")
    store = ArtifactStore(tmp_path / "store")
    store.ingest_artifact(source, _artifact(source))
    event_path = next(store.events_path.glob("*.json"))
    document = json.loads(event_path.read_text())
    document["payload"]["artifact"]["trial_id"] = "tampered"
    event_path.write_text(json.dumps(document))

    with pytest.raises(ArtifactStoreError, match="hash does not verify"):
        store.replay()


def test_replay_detects_a_broken_link_between_valid_event_documents(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "store")
    for artifact_id in ("fit-1", "fit-2"):
        source = tmp_path / f"{artifact_id}.bin"
        source.write_bytes(artifact_id.encode())
        store.ingest_artifact(source, _artifact(source, artifact_id))
    second_path = sorted(store.events_path.glob("*.json"))[1]
    document = json.loads(second_path.read_text())
    document["previous_event_sha256"] = "0" * 64
    second_path.write_text(json.dumps(document))

    with pytest.raises(ArtifactStoreError, match="hash chain is broken"):
        store.replay()


def test_verify_detects_blob_tampering(tmp_path: Path) -> None:
    source = tmp_path / "model.bin"
    source.write_bytes(b"weights")
    store = ArtifactStore(tmp_path / "store")
    ingested = store.ingest_artifact(source, _artifact(source))
    Path(ingested.uri.removeprefix("file://")).write_bytes(b"tampered")

    with pytest.raises(ArtifactStoreError, match="checksum or size"):
        store.verify_artifact(ingested.artifact_id)


def test_projection_failure_leaves_event_replayable_and_rebuild_repairs_view(
    tmp_path: Path,
) -> None:
    source = tmp_path / "model.bin"
    source.write_bytes(b"weights")
    failures = 1

    def projection(snapshot):
        nonlocal failures
        if failures:
            failures -= 1
            raise RuntimeError("crash after event")
        return {"artifact_ids": sorted(snapshot.artifacts)}

    store = ArtifactStore(tmp_path / "store", projection_builders={"artifacts": projection})
    with pytest.raises(RuntimeError, match="crash after event"):
        store.ingest_artifact(source, _artifact(source))

    assert set(store.replay().artifacts) == {"fit-1"}
    assert not (store.projections_path / "artifacts.json").exists()
    store.rebuild_projections()
    assert json.loads((store.projections_path / "artifacts.json").read_text()) == {
        "artifact_ids": ["fit-1"],
    }


def test_concurrent_distinct_ingests_form_one_contiguous_hash_chain(tmp_path: Path) -> None:
    root = tmp_path / "store"
    barrier = Barrier(4)

    def ingest(index: int) -> None:
        source = tmp_path / f"model-{index}.bin"
        source.write_bytes(f"weights-{index}".encode())
        barrier.wait()
        ArtifactStore(root, clock=lambda: float(index)).ingest_artifact(
            source, _artifact(source, f"fit-{index}"),
        )

    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(ingest, range(4)))

    snapshot = ArtifactStore(root).replay()
    assert [event.sequence for event in snapshot.events] == [1, 2, 3, 4]
    assert set(snapshot.artifacts) == {f"fit-{index}" for index in range(4)}
    assert all(
        current.previous_event_sha256 == previous.event_sha256
        for previous, current in zip(snapshot.events, snapshot.events[1:])
    )


def test_nonfinite_event_time_is_refused_before_history_is_written(tmp_path: Path) -> None:
    source = tmp_path / "model.bin"
    source.write_bytes(b"weights")
    store = ArtifactStore(tmp_path / "store", clock=lambda: float("nan"))

    with pytest.raises(ArtifactStoreError, match="occurred_at must be finite"):
        store.ingest_artifact(source, _artifact(source))
    assert not list(store.events_path.glob("*.json"))
