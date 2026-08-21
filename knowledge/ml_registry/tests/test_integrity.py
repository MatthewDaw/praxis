from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from knowledge.ml_registry.services.integrity import audit_registry


class _RegistryProjection:
    def __init__(self, *, repo: Path, sha: str, reason: str = "adopted") -> None:
        self.repo = repo
        self.sha = sha
        self.reason = reason

    def rows(self, table: str):
        if table == "runs":
            return [{"run_id": "run-1", "code_ref": "{}"}]
        if table == "model_versions":
            return [{"model_id": "model", "version": 1, "run_id": "run-1",
                     "code_sha": self.sha}]
        if table == "aliases":
            return [{"model_id": "model", "alias": "champion", "version": 1}]
        raise AssertionError(table)

    def list_events(self):
        return (SimpleNamespace(
            sequence=7, event_type="run_adopted", payload={
                "reason": self.reason,
                "model_version": {"model_id": "model", "version": 1},
            },
        ),)


def test_integrity_reports_missing_commit_and_reasonless_champion_move(tmp_path):
    projection = _RegistryProjection(repo=tmp_path, sha="f" * 40, reason="")
    report = audit_registry(projection, repo=tmp_path)

    assert report.missing_code_shas == (f"model@1:{'f' * 40}",)
    assert report.champion_moves_without_reason_event == ("event:7:model@1",)
    assert report.ok is False


def test_integrity_reports_champion_projection_without_matching_move(tmp_path):
    projection = _RegistryProjection(repo=tmp_path, sha="f" * 40)
    projection.list_events = lambda: ()
    report = audit_registry(projection, repo=tmp_path)

    assert report.champion_moves_without_reason_event == (
        "alias:model@1:missing-move-event",
    )
