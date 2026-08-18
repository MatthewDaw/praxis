"""Tests for campaign bootstrap -- the systematic half of standing a campaign up.

Every fixture is built from arithmetic in a tmp dir; nothing reads a real project.

These pin the failures that cost real time before this module existed. Each one produced a ledger
that LOOKED fine and could not be adjudicated, and each was discovered only at registration, after
the training runs that filled the ledger had been paid for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge.ml_registry.bootstrap import (
    LEDGER_V2_HEADER,
    bootstrap,
    build_ideas,
    check_ledger,
    measure_noise_floor,
)


def _ledger(tmp: Path, rows: list[tuple[str, float, float, str]], header=None) -> Path:
    p = tmp / "results.tsv"
    head = header or LEDGER_V2_HEADER
    lines = ["\t".join(head)]
    for commit, value, tput, desc in rows:
        lines.append("\t".join([commit, f"{value}", "0.0", "ok", desc, f"{tput}", "0"]))
    p.write_text("\n".join(lines) + "\n")
    return p


def _baselines(n: int, start: float = 0.68, step: float = 0.002) -> list[tuple]:
    return [(f"sha:baseline_{i}", start + i * step, 3.0, f"baseline_{i} | head=gru")
            for i in range(n)]


def test_duplicate_join_keys_are_refused(tmp_path: Path) -> None:
    """The failure that cost an afternoon: a campaign varying arms by CONFIG writes every row
    under the same SHA, so the trial->row join collapses and NOTHING can be adjudicated."""
    rows = [("samesha", 0.68 + i * 0.002, 3.0, f"baseline_{i}") for i in range(4)]
    checks, _ = check_ledger(_ledger(tmp_path, rows), "baseline")
    bad = [c for c in checks if c.name == "join_keys_unique"]
    assert bad and not bad[0].ok
    assert "{sha}:{arm_tag}" in bad[0].detail, "the refusal must name the fix, not just the fault"


def test_v1_ledger_is_refused_naming_the_missing_columns(tmp_path: Path) -> None:
    v1 = ["commit", "metric_value", "memory_gb", "status", "description"]
    p = tmp_path / "results.tsv"
    p.write_text("\t".join(v1) + "\nsha:a\t0.68\t0.0\tok\tbaseline_0\n")
    checks, _ = check_ledger(p, "baseline")
    v2 = [c for c in checks if c.name == "ledger_is_v2"][0]
    assert not v2.ok and "throughput" in v2.detail


def test_too_few_baseline_rows_is_refused(tmp_path: Path) -> None:
    checks, _ = check_ledger(_ledger(tmp_path, _baselines(3)), "baseline")
    assert not [c for c in checks if c.name == "enough_baseline_rows"][0].ok


def test_heterogeneous_baseline_throughput_is_flagged(tmp_path: Path) -> None:
    """baseline_throughput gates the VOIDED verdict. A baseline measured under different settings
    -- one seed instead of four, say -- makes that gate meaningless."""
    rows = _baselines(4)
    rows[0] = (rows[0][0], rows[0][1], 9.1, rows[0][3])      # a --seeds 1 probe among 4-seed runs
    checks, _ = check_ledger(_ledger(tmp_path, rows), "baseline")
    assert not [c for c in checks if c.name == "baseline_throughput_homogeneous"][0].ok


def test_noise_floor_reports_its_own_uncertainty() -> None:
    """An SD from 4 points carries ~40% relative uncertainty. Reporting the value without that is
    how a floor measured on 4 runs (0.0164) gets trusted over one measured on 12 (0.0115)."""
    few = measure_noise_floor([{"metric_value": v} for v in (0.68, 0.69, 0.67, 0.70)])
    many = measure_noise_floor([{"metric_value": 0.68 + 0.001 * i} for i in range(13)])
    assert few["sd_relative_uncertainty"] > many["sd_relative_uncertainty"]
    assert few["n_baseline_runs"] == 4


def test_floor_defaults_to_two_sigma() -> None:
    """At one sigma a large backlog manufactures winners from optimiser noise."""
    f = measure_noise_floor([{"metric_value": v} for v in (0.68, 0.69, 0.67, 0.70)])
    assert f["sigmas"] == 2.0
    assert f["noise_floor"] == pytest.approx(2 * f["sd"], rel=1e-6)


def test_ideas_carry_the_basis_not_just_the_hypothesis() -> None:
    """A rejected idea is only useful later if the REASON it was worth trying survives beside the
    verdict; otherwise rejection memory decays into a list of names nobody can re-evaluate."""
    ideas = build_ideas([{"id": "R01", "axis": "representation",
                          "hypothesis": "drop face joints helps",
                          "basis": "measured +0.5 elsewhere"}], model_id="m1")
    assert ideas[0]["model_id"] == "m1"
    assert "R01" in ideas[0]["description"]
    assert "measured +0.5 elsewhere" in ideas[0]["description"]


def test_skip_ids_omits_settled_losers() -> None:
    backlog = [{"id": "R04", "axis": "representation", "hypothesis": "velocity"},
               {"id": "R03", "axis": "representation", "hypothesis": "bones"}]
    ideas = build_ideas(backlog, model_id="m1", skip_ids={"R04"})
    assert [i["description"].split(":")[0] for i in ideas] == ["R03"]


def test_bootstrap_is_not_ready_when_any_precondition_fails(tmp_path: Path) -> None:
    rows = [("samesha", 0.68, 3.0, f"baseline_{i}") for i in range(4)]
    rep = bootstrap(ledger=_ledger(tmp_path, rows), backlog=[], model_id="m",
                    metric="f1", direction="maximize", diff_size_limit=8)
    assert not rep.ready and rep.model_meta is None
    assert "join_keys_unique" in rep.to_dict()["blocking"]


def test_bootstrap_emits_schema_valid_meta_when_ready(tmp_path: Path) -> None:
    from knowledge.ml_registry.schema import REQUIRED_META_KEYS
    rep = bootstrap(ledger=_ledger(tmp_path, _baselines(4)),
                    backlog=[{"id": "R01", "axis": "rep", "hypothesis": "h", "basis": "b"}],
                    model_id="m", metric="f1", direction="maximize", diff_size_limit=8)
    assert rep.ready, rep.to_dict()["blocking"]
    assert not set(REQUIRED_META_KEYS["model"]) - set(rep.model_meta)
    assert not set(REQUIRED_META_KEYS["idea"]) - set(rep.ideas[0])
    # Nearest the baselines' MEAN, not the max: they are repeats of one config, so taking
    # the best of them selects on noise. Values here are 0.680/0.682/0.684/0.686, mean 0.683;
    # baseline_1 and baseline_2 are equidistant and the tie breaks on ledger order.
    assert rep.model_meta["baseline"] == "sha:baseline_1"


def test_baseline_is_the_row_nearest_the_mean_not_the_best_one(tmp_path) -> None:
    """Baseline rows are REPEATS of one config, so taking their max selects on noise.

    Regression for the first real campaign: rows at 0.6700/0.6795/0.6809/0.6811 registered the
    0.6811 one, 0.6 sigma above their own mean, making every arm clear a bar the baseline config
    could not reliably clear itself. E[max of 4 normal draws] is about mu + 1.03*sigma.
    """
    from knowledge.ml_registry.bootstrap import bootstrap

    ledger = tmp_path / "results.tsv"
    rows = [("baseline_3", 0.6809), ("baseline_4", 0.6700),
            ("baseline_5", 0.6811), ("baseline_6", 0.6795)]
    ledger.write_text(
        "commit\tmetric_value\tmemory_gb\tstatus\tdescription\tthroughput\tdiff_lines\n"
        + "".join(f"sha:{tag}\t{v}\t0.0\tok\t{tag} | head=gru\t3.48\t0\n" for tag, v in rows))
    backlog = [{"id": "R01", "axis": "representation", "description": "d"}]

    report = bootstrap(ledger=ledger, backlog=backlog, model_id="m", metric="f1",
                       direction="maximize", diff_size_limit=8, baseline_prefix="baseline")
    assert report.ready
    # mean is 0.677875; baseline_6 (0.6795) is nearest it, baseline_5 (0.6811) is the max
    assert report.model_meta["baseline"] == "sha:baseline_6"


def test_meta_json_accepts_a_path_so_bootstrap_output_can_be_registered(tmp_path) -> None:
    """The seam between the workflow's two halves: bootstrap WRITES files, register-* reads them.

    `--meta-json` only ever parsed a literal JSON string, so the documented sequence
    (bootstrap-campaign, then register-model-with-baseline --meta-json <meta>.json) failed with
    "MALFORMED INPUT: Expecting value: line 1 column 1", naming neither the argument nor the cause.
    """
    from knowledge.ml_registry.cli import _json_arg

    meta = tmp_path / "model_meta.json"
    meta.write_text('{"metric": "f1", "direction": "maximize"}')

    assert _json_arg(str(meta)) == {"metric": "f1", "direction": "maximize"}
    assert _json_arg('{"metric": "f1"}') == {"metric": "f1"}          # literal still works
    assert _json_arg('  {"metric": "f1"}  ') == {"metric": "f1"}      # and is whitespace tolerant

    # A missing path reports a FILE problem, not a parse problem -- the distinction the
    # leading-brace check exists to preserve.
    try:
        _json_arg(str(tmp_path / "absent.json"))
    except ValueError as exc:
        assert "existing file" in str(exc)
    else:
        raise AssertionError("expected a ValueError naming the missing file")
