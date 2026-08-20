from __future__ import annotations

from dataclasses import asdict
import json

import pytest

from knowledge.ml_registry.artifact_cache import (
    ArtifactCacheIndex,
    CacheKey,
    load_index,
    main,
    save_index,
)
from knowledge.ml_registry.schema import RegistryValidationError


KEY = CacheKey("fit-1", "weights-1", "data-sha", "folds-v1", "prep-v2", "features-v3")


def test_register_and_exact_lookup():
    index = ArtifactCacheIndex()
    entry = index.register(
        KEY,
        uri="s3://bucket/preds",
        checksum="sha256:abc",
        coverage=0.99,
        prediction_scope="oof",
    )
    assert (
        index.lookup(
            KEY, require_oof=True, minimum_coverage=0.95, expected_checksum="sha256:abc"
        )
        == entry
    )


@pytest.mark.parametrize(
    "field",
    [
        "upstream_fit_id",
        "upstream_artifact_id",
        "dataset_manifest",
        "split",
        "preprocessing",
        "feature_schema",
    ],
)
def test_every_lineage_field_changes_the_key(field):
    values = KEY.__dict__.copy()
    values[field] += "-changed"
    assert CacheKey(**values).id != KEY.id


def test_in_fold_predictions_are_never_reused_for_oof():
    index = ArtifactCacheIndex()
    index.register(
        KEY,
        uri="file:///preds",
        checksum="sha256:x",
        coverage=1,
        prediction_scope="in_fold",
    )
    with pytest.raises(RegistryValidationError, match="in-fold"):
        index.lookup(KEY, require_oof=True)


def test_coverage_and_checksum_are_verified():
    index = ArtifactCacheIndex()
    index.register(
        KEY,
        uri="file:///preds",
        checksum="sha256:x",
        coverage=0.8,
        prediction_scope="oof",
    )
    with pytest.raises(RegistryValidationError) as coverage:
        index.lookup(KEY, minimum_coverage=0.9)
    assert coverage.value.field == "coverage"
    with pytest.raises(RegistryValidationError) as checksum:
        index.lookup(KEY, expected_checksum="sha256:y")
    assert checksum.value.field == "checksum"


def test_superseding_preserves_immutable_history():
    index = ArtifactCacheIndex()
    old = index.register(
        KEY, uri="s3://old", checksum="sha256:old", coverage=0.9, prediction_scope="oof"
    )
    new = index.register(
        KEY, uri="s3://new", checksum="sha256:new", coverage=1, prediction_scope="oof"
    )
    assert old.entry_id in index.entries
    assert index.superseded[old.entry_id] == new.entry_id
    assert index.lookup(KEY) == new


def test_invalidation_keeps_history_but_refuses_lookup():
    index = ArtifactCacheIndex()
    entry = index.register(
        KEY, uri="s3://x", checksum="sha256:x", coverage=1, prediction_scope="oof"
    )
    index.invalidate(entry.entry_id, reason="upstream retired")
    assert entry.entry_id in index.entries
    with pytest.raises(RegistryValidationError, match="no active"):
        index.lookup(KEY)


def test_atomic_round_trip_and_tamper_detection(tmp_path):
    path = tmp_path / "cache.json"
    index = ArtifactCacheIndex()
    entry = index.register(
        KEY, uri="s3://x", checksum="sha256:x", coverage=1, prediction_scope="oof"
    )
    save_index(path, index)
    assert load_index(path).lookup(KEY) == entry
    raw = json.loads(path.read_text())
    raw["entries"][entry.entry_id]["checksum"] = "tampered"
    path.write_text(json.dumps(raw))
    with pytest.raises(RegistryValidationError, match="content hash"):
        load_index(path)


def test_cli_register_lookup_and_refusal(tmp_path, capsys):
    path = tmp_path / "cache.json"
    key = json.dumps(KEY.__dict__)
    assert (
        main(
            [
                "--index",
                str(path),
                "register",
                "--key-json",
                key,
                "--uri",
                "s3://x",
                "--checksum",
                "sha256:x",
                "--coverage",
                "1",
                "--prediction-scope",
                "oof",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "--index",
                str(path),
                "lookup",
                "--key-json",
                key,
                "--require-oof",
                "--minimum-coverage",
                ".9",
                "--expected-checksum",
                "sha256:x",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["uri"] == "s3://x"
    assert (
        main(
            [
                "--index",
                str(path),
                "lookup",
                "--key-json",
                key,
                "--expected-checksum",
                "wrong",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["status"] == "refused"


def _persisted(tmp_path, **overrides):
    index = ArtifactCacheIndex()
    entry = index.register(
        KEY, uri="s3://b/p", checksum="sha256:abc", coverage=0.9, prediction_scope="oof"
    )
    path = tmp_path / "cache.json"
    save_index(path, index)
    document = json.loads(path.read_text())
    if overrides:
        document["entries"][entry.entry_id].update(overrides)
    return path, document, entry


@pytest.mark.parametrize("coverage", [7.0, float("nan"), True, "0.9"])
def test_persisted_coverage_is_revalidated_on_load(tmp_path, coverage):
    path, document, entry = _persisted(tmp_path)
    document["entries"][entry.entry_id]["coverage"] = coverage
    path.write_text(json.dumps(document, allow_nan=True))

    with pytest.raises(RegistryValidationError) as error:
        load_index(path)
    assert error.value.field == "coverage"


def test_persisted_prediction_scope_is_revalidated_on_load(tmp_path):
    path, document, entry = _persisted(tmp_path)
    document["entries"][entry.entry_id]["prediction_scope"] = "bogus"
    path.write_text(json.dumps(document))

    with pytest.raises(RegistryValidationError) as error:
        load_index(path)
    assert error.value.field == "prediction_scope"


def test_active_pointer_to_a_missing_entry_is_refused(tmp_path):
    path, document, _ = _persisted(tmp_path)
    document["active"] = {"some-key": "no-such-entry"}
    path.write_text(json.dumps(document))

    with pytest.raises(RegistryValidationError, match="unknown cache entry"):
        load_index(path)


def test_active_pointer_whose_key_does_not_match_its_entry_is_refused(tmp_path):
    path, document, entry = _persisted(tmp_path)
    document["active"] = {"wrong-key-id": entry.entry_id}
    path.write_text(json.dumps(document))

    with pytest.raises(RegistryValidationError, match="does not match entry key"):
        load_index(path)


def test_cli_lookup_refuses_a_tampered_index_rather_than_exiting_zero(tmp_path, capsys):
    path, document, entry = _persisted(tmp_path)
    document["entries"][entry.entry_id]["coverage"] = 7.0
    path.write_text(json.dumps(document))

    code = main([
        "--index", str(path), "lookup",
        "--key-json", json.dumps(asdict(KEY)),
        "--require-oof", "--minimum-coverage", "0.5",
    ])

    assert code == 2
    assert json.loads(capsys.readouterr().err)["field"] == "coverage"
