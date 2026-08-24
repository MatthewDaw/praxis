from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from knowledge.ml_registry.baselines import (
    INCUMBENT_RUNG,
    BaselineCandidate,
    BaselineReproductionError,
    reproduce_baselines,
)
from knowledge.ml_registry.contracts.ledger_v2 import LEDGER_V2_HEADER

_CANDIDATES = (
    BaselineCandidate("botsort", "tracking-by-detection", "https://example.test/botsort", 1),
    BaselineCandidate("timm-convnext", "direct-regression", "https://example.test/timm", 2),
    BaselineCandidate("kp-homography", "keypoint-plus-homography", "https://example.test/kp", 3),
)


def _ledger(tmp_path: Path, rows: Sequence[tuple[str, float, str]]) -> Path:
    path = tmp_path / "results.tsv"
    lines = ["\t".join(LEDGER_V2_HEADER)]
    lines.extend(
        "\t".join([commit, f"{value}", "0.0", status, f"{commit} baseline", "3.0", "0"])
        for commit, value, status in rows
    )
    path.write_text("\n".join(lines) + "\n")
    return path


def _reproduce(candidate: BaselineCandidate) -> str:
    return f"sha:{candidate.id}"


def test_campaign_yields_three_measured_baselines_from_different_families(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, [
        ("sha:botsort", 0.60, "ok"),
        ("sha:timm-convnext", 0.61, "ok"),
        ("sha:kp-homography", 0.62, "ok"),
    ])

    suite = reproduce_baselines("campaign-7", _CANDIDATES, _reproduce, ledger)

    assert suite.complete is True
    assert len(suite.measured) == 3
    assert suite.families == frozenset({
        "tracking-by-detection", "direct-regression", "keypoint-plus-homography",
    })
    assert suite.to_dict()["status"] == "complete"
    # Every score is the ledger's, not a number the reproducer reported about its own run.
    assert [entry.score for entry in suite.ranking()] == [0.62, 0.61, 0.60]


def test_incumbent_enters_as_a_rung_zero_candidate_on_the_same_path(tmp_path: Path) -> None:
    incumbent = BaselineCandidate(
        "production-homography", "keypoint-plus-homography", "https://example.test/prod", 2,
    )
    ledger = _ledger(tmp_path, [
        ("sha:botsort", 0.60, "ok"),
        ("sha:timm-convnext", 0.61, "ok"),
        ("sha:kp-homography", 0.62, "ok"),
        ("sha:production-homography", 0.65, "ok"),
    ])
    reproduced: list[str] = []

    def record(candidate: BaselineCandidate) -> str:
        reproduced.append(candidate.id)
        return _reproduce(candidate)

    suite = reproduce_baselines("campaign-7", _CANDIDATES, record, ledger, incumbent=incumbent)

    # Warm start: the incumbent is reproduced and scored by the same path as every challenger.
    assert reproduced == ["production-homography", "botsort", "timm-convnext", "kp-homography"]
    top = suite.ranking()[0]
    assert top.candidate.id == "production-homography"
    assert top.candidate.rung == INCUMBENT_RUNG
    assert top.score == 0.65
    assert suite.to_dict()["ranking"][0]["rung"] == 0


def test_rung_zero_is_reserved_for_the_incumbent(tmp_path: Path) -> None:
    impostor = BaselineCandidate("challenger", "direct-regression", "https://example.test/c", 0)

    with pytest.raises(BaselineReproductionError, match="rung 0"):
        reproduce_baselines("campaign-7", [impostor], _reproduce, _ledger(tmp_path, []))


def test_every_reproduction_failure_is_recorded_rather_than_dropped(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, [("sha:timm-convnext", 0.61, "aborted")])

    def flaky(candidate: BaselineCandidate) -> str:
        if candidate.id == "botsort":
            raise RuntimeError("pinned dependency does not build")
        return _reproduce(candidate)

    suite = reproduce_baselines("campaign-7", _CANDIDATES, flaky, ledger)

    assert suite.complete is False
    artifact = suite.to_dict()
    assert artifact["status"] == "failed"
    ranked = {row["id"]: row for row in artifact["ranking"]}
    assert set(ranked) == {"botsort", "timm-convnext", "kp-homography"}
    assert all(row["status"] == "unreproduced" for row in ranked.values())
    assert "does not build" in ranked["botsort"]["unreproduced_reason"]
    assert "aborted" in ranked["timm-convnext"]["unreproduced_reason"]
    assert "no scored row" in ranked["kp-homography"]["unreproduced_reason"]


def test_an_unreproduced_baseline_keeps_its_place_below_the_measured_ones(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, [
        ("sha:botsort", 0.60, "ok"),
        ("sha:timm-convnext", 0.61, "ok"),
    ])

    suite = reproduce_baselines("campaign-7", _CANDIDATES, _reproduce, ledger)

    assert suite.complete is False
    assert suite.to_dict()["status"] == "incomplete"
    assert [entry.candidate.id for entry in suite.ranking()] == [
        "timm-convnext", "botsort", "kp-homography",
    ]
