"""Tests for campaign bootstrap -- the systematic half of standing a campaign up.

Every fixture is built from arithmetic in a tmp dir; nothing reads a real project.

These pin the failures that cost real time before this module existed. Each one produced a ledger
that LOOKED fine and could not be adjudicated, and each was discovered only at registration, after
the training runs that filled the ledger had been paid for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowledge.ml_registry.bootstrap import (
    LEDGER_V2_HEADER,
    bootstrap,
    build_ideas,
    check_ledger,
    measure_rope,
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


def test_the_rope_reports_its_own_uncertainty() -> None:
    """An SD from 4 points carries ~40% relative uncertainty. Reporting the value without that is
    how a bar measured on 4 runs (0.0164) gets trusted over one measured on 12 (0.0115)."""
    few = measure_rope([{"metric_value": v} for v in (0.68, 0.69, 0.67, 0.70)])
    many = measure_rope([{"metric_value": 0.68 + 0.001 * i} for i in range(13)])
    assert few["sd_relative_uncertainty"] > many["sd_relative_uncertainty"]
    assert few["n_baseline_runs"] == 4


def test_the_rope_defaults_to_one_sigma_and_says_what_that_costs() -> None:
    """ONE sigma is the standing default, and a deliberate trade rather than an oversight: a
    null arm clears it 15.9% of the time one-sided (2.3% at two sigma), ~10 expected false
    adoptions over a 66-idea backlog rather than ~1.5. It is accepted because a two-sigma bar
    over a noisy metric is one nothing can clear -- detection ran 34 trials and adopted
    nothing. The note must carry that reasoning, because the number alone cannot."""
    f = measure_rope([{"metric_value": v} for v in (0.68, 0.69, 0.67, 0.70)])
    assert f["sigmas"] == 1.0
    assert f["rope"] == pytest.approx(f["sd"], rel=1e-6)
    assert "15.9%" in f["note"] and "ratchet" in f["note"]


def test_two_sigma_is_one_field_away() -> None:
    """A campaign that wants the old bar back must not have to compute it by hand."""
    f = measure_rope([{"metric_value": v} for v in (0.68, 0.69, 0.67, 0.70)], sigmas=2.0)
    assert f["sigmas"] == 2.0
    assert f["rope"] == pytest.approx(2 * f["sd"], rel=1e-6)


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
                    model_id="m", metric="f1", direction="maximize", diff_size_limit=8,
                    win_condition={"metric_at_least": 0.9})
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
                       direction="maximize", diff_size_limit=8, baseline_prefix="baseline",
                       win_condition={"metric_at_least": 0.9})
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


def test_baseline_throughput_is_the_slowest_baseline_not_the_median(tmp_path) -> None:
    """The VOID gate must sit BELOW the healthy range, or healthy arms void indefinitely.

    Regression for the first campaign to run this path: baselines at 3.38/3.47/3.49/3.49 are a
    3.2% spread against a 5% gate. Registering the median (3.48) put the void line at 3.306, and
    the first real arm voided at 3.30 -- missing by 0.2%. The slowest baseline was itself only
    2.2% clear of its own void line.
    """
    from knowledge.ml_registry.bootstrap import bootstrap

    ledger = tmp_path / "results.tsv"
    rows = [("baseline_1", 0.6809, 3.49), ("baseline_2", 0.6700, 3.47),
            ("baseline_3", 0.6811, 3.49), ("baseline_4", 0.6795, 3.38)]
    ledger.write_text(
        "commit\tmetric_value\tmemory_gb\tstatus\tdescription\tthroughput\tdiff_lines\n"
        + "".join(f"sha:{t}\t{v}\t0.0\tok\t{t} | head=gru\t{tp}\t0\n" for t, v, tp in rows))

    report = bootstrap(ledger=ledger, backlog=[{"id": "R01", "axis": "representation",
                                                "description": "d"}],
                       model_id="m", metric="f1", direction="maximize",
                       diff_size_limit=8, baseline_prefix="baseline",
                       win_condition={"metric_at_least": 0.9})
    assert report.ready
    assert report.model_meta["baseline_throughput"] == 3.38     # min, not median 3.48
    # the arm that voided under the median must now clear the gate
    assert 3.30 >= report.model_meta["baseline_throughput"] * 0.95


def test_bootstrap_emits_baseline_runs_and_sigmas(tmp_path: Path) -> None:
    """register-model-with-baseline consumes these; without them the documented path refuses."""
    report = bootstrap(ledger=_ledger(tmp_path, _baselines(4)),
                       backlog=[{"id": "R01", "axis": "representation", "hypothesis": "h"}],
                       model_id="m", metric="f1", direction="maximize", diff_size_limit=8,
                       win_condition={"metric_at_least": 0.9})
    assert report.ready
    assert report.model_meta["sigmas"] == 1.0
    assert len(report.model_meta["baseline_runs"]) == 4
    assert report.model_meta["baseline_runs"] == [row[0] for row in _baselines(4)]


# --- B2: what "winning" means is asked for, never defaulted ---------------------------


def test_build_model_meta_refuses_a_missing_win_condition() -> None:
    """It defaulted to the WIN_ON_ADOPTION string, which closes the campaign as WON on the
    first adopted trial with every other declared stage untried."""
    from knowledge.ml_registry.bootstrap import build_model_meta
    from knowledge.ml_registry.schema import RegistryValidationError

    kwargs = dict(metric="f1", direction="maximize", baseline_commit="sha",
                  baseline_throughput=3.0, diff_size_limit=8)
    with pytest.raises(TypeError):
        build_model_meta(**kwargs)  # type: ignore[arg-type]
    with pytest.raises(RegistryValidationError) as excinfo:
        build_model_meta(**kwargs, win_condition=None)
    assert excinfo.value.field == "win_condition"
    message = str(excinfo.value)
    assert "metric_at_most" in message and "metric_at_least" in message
    assert "first" in message.lower() and "supervisor.py" in message


def test_build_model_meta_refuses_the_bare_adoption_string() -> None:
    from knowledge.ml_registry.bootstrap import WIN_ON_ADOPTION, build_model_meta
    from knowledge.ml_registry.schema import RegistryValidationError

    with pytest.raises(RegistryValidationError) as excinfo:
        build_model_meta(metric="f1", direction="maximize", baseline_commit="sha",
                         baseline_throughput=3.0, diff_size_limit=8,
                         win_condition=WIN_ON_ADOPTION)
    assert excinfo.value.field == "win_condition"


def test_bootstrap_is_not_ready_without_a_declared_win_condition(tmp_path: Path) -> None:
    report = bootstrap(ledger=_ledger(tmp_path, _baselines(4)),
                       backlog=[{"id": "R01", "axis": "rep", "hypothesis": "h"}],
                       model_id="m", metric="f1", direction="maximize", diff_size_limit=8)
    assert not report.ready and report.model_meta is None
    assert "win_condition_declared" in report.to_dict()["blocking"]


def test_bootstrap_carries_the_declared_win_condition_onto_the_meta(tmp_path: Path) -> None:
    report = bootstrap(ledger=_ledger(tmp_path, _baselines(4)),
                       backlog=[{"id": "R01", "axis": "rep", "hypothesis": "h"}],
                       model_id="m", metric="f1", direction="maximize", diff_size_limit=8,
                       win_condition={"metric_at_least": 0.72})
    assert report.ready, report.to_dict()["blocking"]
    assert report.model_meta["win_condition"] == {"metric_at_least": 0.72}


def test_identical_baseline_rows_bootstrap_a_campaign_and_report_a_zero_rope(tmp_path: Path) -> None:
    """A DETERMINISTIC incumbent -- classical CV, no random seed -- produces four identical
    rows. That used to block the bootstrap, because the zero it measures was about to be
    stored as a threshold. Nothing is stored now, so the measurement is simply reported:
    the campaign is set up, and a positive bar for such a model is measured over its
    scoring corpus (policy_gate.compute_campaign_rope) rather than over repeats that cannot
    vary."""
    rows = [(f"sha:baseline_{i}", 0.42, 3.0, f"baseline_{i}") for i in range(4)]
    report = bootstrap(
        ledger=_ledger(tmp_path, rows), backlog=[], model_id="m",
        metric="f1", direction="maximize", diff_size_limit=8,
        win_condition={"metric_at_least": 0.7})

    assert report.ready, report.to_dict()["blocking"]
    assert report.to_dict()["rope"]["rope"] == 0.0
    assert report.model_meta["baseline_runs"] == [f"sha:baseline_{i}" for i in range(4)]


def test_the_model_meta_carries_the_ropes_evidence_and_no_threshold(tmp_path: Path) -> None:
    """What bootstrap hands the registry is the baseline commits, not a number: the rope is
    recomputed from their ledger rows at every comparison."""
    report = bootstrap(ledger=_ledger(tmp_path, _baselines(4)),
                       backlog=[{"id": "R01", "axis": "rep", "hypothesis": "h"}],
                       model_id="m", metric="f1", direction="maximize", diff_size_limit=8,
                       win_condition={"metric_at_least": 0.72})
    assert report.ready, report.to_dict()["blocking"]
    assert len(report.model_meta["baseline_runs"]) == 4
    assert not [key for key in report.model_meta if "floor" in key]
