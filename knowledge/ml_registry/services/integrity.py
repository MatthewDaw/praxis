"""Read-only integrity audit for registry code provenance and champion history."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Any

from knowledge.ml_registry.storage.registry import Registry


@dataclass(frozen=True)
class RegistryIntegrityReport:
    missing_code_shas: tuple[str, ...]
    champion_moves_without_reason_event: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.missing_code_shas and not self.champion_moves_without_reason_event


def _commit_exists(repo: Path, sha: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    return result.returncode == 0


def _champion_move(event: Any) -> tuple[str, int] | None:
    payload = event.payload
    if event.event_type in {"run_adopted", "run_baselined"}:
        version = payload.get("model_version")
        if isinstance(version, dict):
            return str(version.get("model_id", "")), int(version.get("version", 0))
    if event.event_type == "alias_set" and payload.get("alias") == "champion":
        return str(payload.get("model_id", "")), int(payload.get("version", 0))
    if event.event_type == "adoption_invalidated":
        return str(payload.get("model_id", "")), int(payload.get("parent_version", 0))
    return None


def audit_registry(registry: Registry, *, repo: str | Path | None = None) -> RegistryIntegrityReport:
    """Prove model-version commits exist and champion moves carry durable reasons."""
    runs = {row["run_id"]: row for row in registry.rows("runs")}
    missing: list[str] = []
    for version in registry.rows("model_versions"):
        identity = f"{version['model_id']}@{version['version']}:{version['code_sha']}"
        run = runs.get(version["run_id"])
        try:
            code_ref = json.loads(run["code_ref"]) if run is not None else {}
        except (TypeError, json.JSONDecodeError):
            code_ref = {}
        intended_repo = code_ref.get("repo") or repo
        if intended_repo is None or not _commit_exists(Path(intended_repo), version["code_sha"]):
            missing.append(identity)

    reasonless: list[str] = []
    last_moves: dict[str, tuple[int, int]] = {}
    for event in registry.list_events():
        move = _champion_move(event)
        if move is None:
            continue
        last_moves[move[0]] = (move[1], event.sequence)
        reason = event.payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            reasonless.append(f"event:{event.sequence}:{move[0]}@{move[1]}")

    for alias in registry.rows("aliases"):
        if alias["alias"] != "champion":
            continue
        move = last_moves.get(alias["model_id"])
        identity = f"{alias['model_id']}@{alias['version']}"
        if move is None:
            reasonless.append(f"alias:{identity}:missing-move-event")
        elif move[0] != alias["version"]:
            reasonless.append(f"alias:{identity}:last-event-{move[1]}-mismatch")

    return RegistryIntegrityReport(tuple(sorted(missing)), tuple(sorted(reasonless)))
