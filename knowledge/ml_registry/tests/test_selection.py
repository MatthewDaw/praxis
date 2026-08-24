"""R3a acceptance: Phase 2 SELECT -- the reuse-rung ladder as a first-class field, the
N-way comparison against a per-comparison rope, the tie policy that resolves DOWN the
ladder, and the retirement of the stored noise-floor field."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from knowledge.ml_registry.floor import register_model_with_baseline
from knowledge.ml_registry.schema import MODEL, REQUIRED_META_KEYS, RegistryValidationError
from knowledge.ml_registry.selection import REUSE_RUNGS, Candidate, select
from knowledge.ml_registry.storage import Registry
from knowledge.ml_registry.write_path import RegistrySpace

POLICY_FIXTURES = Path(__file__).parent / "fixtures" / "policy_gate"
#: Spelled apart so this file's own source does not trip the retirement scan below.
RETIRED_FIELD = "noise" + "_floor"


def _candidate(candidate_id: str, rung: int, value: float, sigma: float = 0.0,
               family: str = "") -> Candidate:
    return Candidate(candidate_id=candidate_id, rung=rung, value=value, sigma=sigma,
                     family=family or candidate_id)


def test_n_candidates_with_known_sigma_select_the_best_one() -> None:
    """Five arms measured on the same split, each with a known sigma well inside the rope:
    the comparison is N-way (no champion is consulted) and the best arm wins on margin."""
    rope = 0.01
    candidates = [
        _candidate("adapter", rung=1, value=0.70, sigma=0.002),
        _candidate("finetune", rung=2, value=0.74, sigma=0.002),
        _candidate("from-paper", rung=3, value=0.81, sigma=0.002),
        _candidate("novel", rung=4, value=0.77, sigma=0.002),
        _candidate("incumbent", rung=0, value=0.62, sigma=0.002),
    ]

    selection = select(candidates, direction="maximize", rope=rope)

    assert selection.winner == "from-paper"
    assert selection.tied == ("from-paper",)
    assert selection.resolved_by == "margin"
    assert selection.ranked == ("from-paper", "novel", "finetune", "adapter", "incumbent")
    assert selection.rope == rope


def test_two_candidates_inside_the_rope_report_tied_and_resolve_to_the_lower_rung() -> None:
    """The shiniest result does not win a tie: among arms within one rope of the top, the
    LOWEST rung wins, which is the rule that makes the ladder real."""
    candidates = [
        _candidate("novel", rung=4, value=0.812, sigma=0.001),
        _candidate("adapter", rung=1, value=0.808, sigma=0.001),
        _candidate("outclassed", rung=1, value=0.500, sigma=0.001),
    ]

    selection = select(candidates, direction="maximize", rope=0.01)

    assert selection.tied == ("novel", "adapter")
    assert selection.winner == "adapter"
    assert selection.resolved_by == "rung"


def test_a_deterministic_candidate_with_zero_sigma_does_not_refuse() -> None:
    """A deterministic arm yields sigma 0. Selection must still return a verdict, and a
    model whose baseline repeats are identical must still register."""
    candidates = [
        _candidate("deterministic", rung=1, value=0.50, sigma=0.0),
        _candidate("stochastic", rung=4, value=0.62, sigma=0.004),
    ]

    selection = select(candidates, direction="maximize", rope=0.005)

    assert selection.winner == "stochastic"
    assert selection.tied == ("stochastic",)

    space = RegistrySpace()
    identical = {"d1": 1.0, "d2": 1.0, "d3": 1.0, "d4": 1.0}
    model_id = register_model_with_baseline(
        space,
        {
            "metric": "val_bpb", "direction": "minimize", "win_condition": "beats baseline",
            "baseline": "d1", "diff_size_limit": 800, "baseline_runs": list(identical),
        },
        identical,
    )
    assert space.get(model_id).meta["baseline_runs"] == list(identical)


def test_a_cold_start_campaign_with_no_champion_computes_a_rope_and_selects(
    tmp_path: Path,
) -> None:
    """No incumbent, no champion alias, no baseline repeats: the rope comes from the
    scoring corpus's own split_unit bootstrap, so SELECT is defined from the first
    measurement."""
    spec = json.loads((POLICY_FIXTURES / "campaign.json").read_text())
    rows = [json.loads(line) for line in (POLICY_FIXTURES / "scoring.jsonl").read_text().splitlines()]
    registry = Registry(tmp_path)

    assert registry.register_campaign_spec(deepcopy(spec), scoring_corpora={"fixture_scoring": rows})
    rope = registry.list_events()[-1].payload["rope"]
    assert rope["value"] > 0

    selection = select(
        [
            _candidate("keypoint-homography", rung=3, value=0.61, sigma=0.01, family="keypoint"),
            _candidate("direct-regression", rung=2, value=0.55, sigma=0.01, family="regression"),
        ],
        direction="maximize",
        rope=float(rope["value"]),
    )

    assert selection.winner == "keypoint-homography"
    assert selection.runner_up == "direct-regression"


def test_the_ladder_is_a_closed_first_class_field_not_opaque_text() -> None:
    """A rung outside the ladder is refused by name -- the point of promoting it out of the
    registry's free-text family column."""
    assert sorted(REUSE_RUNGS) == [0, 1, 2, 3, 4]

    with pytest.raises(RegistryValidationError) as exc_info:
        select([_candidate("mystery", rung=7, value=0.5)], direction="maximize", rope=0.01)

    assert exc_info.value.field == "rung"
    assert "7" in str(exc_info.value)


def test_the_stored_noise_floor_field_no_longer_exists_and_nothing_reads_it() -> None:
    """Two thresholds must not decide one verdict: the rope is computed per comparison and
    the value stored at registration is gone from the schema and from every source file."""
    assert RETIRED_FIELD not in REQUIRED_META_KEYS[MODEL]

    root = Path(__file__).resolve().parents[3]
    offenders = sorted(
        str(path.relative_to(root))
        for directory in ("knowledge", "agent_factory")
        for path in (root / directory).rglob("*.py")
        if RETIRED_FIELD in path.read_text(encoding="utf-8")
    )
    assert offenders == []
