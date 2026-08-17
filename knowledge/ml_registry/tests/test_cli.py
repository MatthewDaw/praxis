"""The shipped entrypoint's own integrity properties (:mod:`knowledge.ml_registry.cli`).

``agent_factory/tests/test_ml_registry_smoke.py`` proves the CLI RUNS as a subprocess. This
module proves the things that make what it runs trustworthy, in-process:

* every acceptance signal is joined out of the real external ledger (``results.tsv``) --
  never a JSON blob the caller wrote, never a value the judged agent reported, and never a
  column the CLI synthesized because the ledger did not carry it;
* a refusal part-way through a multi-write run does not erase the writes the run really made;
* the resolution OUTCOME of a citation is not a caller input;
* updating a registered model is a guarded, merging mutation, not a wholesale replace that
  drops the derived campaign state the registry recomputes its counters from;
* a campaign budget is never silently unlimited;
* R9's keep-pushing marker and R17's lesson filing are reachable from the shipped entrypoint.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge.ml_registry.cli import main

# A ledger carrying everything a verdict is decided on: the metric value, the throughput the
# run actually held, and its net diff lines.
FULL_LEDGER = (
    "commit\tval_bpb\tmemory_gb\tstatus\tdescription\tthroughput\tdiff_lines\n"
    "base\t1.0\t2.0\tok\tbaseline\t1200\t0\n"
    "lose1\t5000.0\t2.0\tok\tclear loss\t1200\t10\n"
    "lose2\t5000.0\t2.0\tok\tclear loss\t1200\t10\n"
    "win1\t0.1\t2.0\tok\tclear win\t1200\t20\n"
    "slow1\t0.1\t2.0\tok\tthroughput collapsed\t100\t20\n"
    "fat1\t1.0\t2.0\tok\tstagnant but enormous\t1200\t5000\n"
)

# The legacy ledger shape: no throughput, no diff_lines.
VALUE_ONLY_LEDGER = (
    "commit\tval_bpb\tmemory_gb\tstatus\tdescription\n"
    "base\t1.0\t2.0\tok\tbaseline\n"
    "lose1\t5000.0\t2.0\tok\tclear loss\n"
)

MODEL_META: dict[str, object] = {
    "metric": "val_bpb",
    "direction": "minimize",
    "win_condition": "beats baseline by noise_floor",
    "baseline": "base",
    "noise_floor": 0.01,
    "baseline_throughput": 1200,
    "diff_size_limit": 800,
}


def _cli(*args: object) -> int:
    return main([str(a) for a in args])


def _out(capsys) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


def _facts(space_file: Path, category: str | None = None) -> list[dict]:
    space = json.loads(space_file.read_text())
    return [f for f in space["facts"] if category is None or f["category"] == category]


@pytest.fixture()
def ledger(tmp_path: Path) -> Path:
    path = tmp_path / "results.tsv"
    path.write_text(FULL_LEDGER)
    return path


@pytest.fixture()
def space_file(tmp_path: Path) -> Path:
    return tmp_path / "space.json"


def _register_model(space_file: Path, capsys, **overrides: object) -> str:
    assert _cli("register-model", "--space-file", space_file, "--meta-json",
                json.dumps({**MODEL_META, **overrides})) == 0
    return _out(capsys).strip().rsplit(" ", 1)[-1]


def _register_idea(space_file: Path, capsys, model_id: str, description: str = "try rope",
                   axis: str = "architecture", origin: str = "seeded") -> str:
    meta = {"model_id": model_id, "origin": origin, "axis": axis, "description": description}
    assert _cli("register-idea", "--space-file", space_file, "--meta-json", json.dumps(meta)) == 0
    return _out(capsys).strip().rsplit(" ", 1)[-1]


def _register_trial(space_file: Path, ledger: Path, capsys, model_id: str, idea_id: str,
                    commit: str, **extra: object) -> str:
    meta = {"model_id": model_id, "idea_id": idea_id, "commit": commit, "status": "running", **extra}
    assert _cli("register-trial", "--space-file", space_file, "--meta-json", json.dumps(meta),
                "--ledger", ledger) == 0
    return _out(capsys).strip().rsplit(" ", 1)[-1]


def _script(tmp_path: Path, name: str, payload: object) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return path


# --- FINDING 3: the verdict's ledger is the loop's results.tsv, not a caller's JSON --------


def test_resolve_verdict_takes_no_caller_supplied_json_ledger(space_file: Path, tmp_path: Path) -> None:
    """The judged agent must not be able to hand the adjudicator the file it will be judged
    against. ``--ledger-json`` accepted exactly that, so it no longer exists."""
    blob = _script(tmp_path, "ledger.json", {"win1": {"value": 0.1, "throughput": 1200, "diff_lines": 0}})
    with pytest.raises(SystemExit) as exc:
        _cli("resolve-verdict", "--space-file", space_file, "--trial-id", "trial-x",
             "--ledger-json", blob)
    assert exc.value.code == 2


def test_resolve_verdict_decides_on_the_real_results_tsv(space_file: Path, ledger: Path, capsys) -> None:
    model_id = _register_model(space_file, capsys)
    idea_id = _register_idea(space_file, capsys, model_id)
    trial_id = _register_trial(space_file, ledger, capsys, model_id, idea_id, "win1",
                               throughput=1200, diff_lines=20)

    assert _cli("resolve-verdict", "--space-file", space_file, "--trial-id", trial_id,
                "--ledger", ledger) == 0
    assert "adopted" in _out(capsys)
    model = next(f for f in _facts(space_file, "model") if f["id"] == model_id)
    assert model["meta"]["baseline"] == "win1"
    assert model["meta"]["previous_baseline"] == "base"


def test_a_verdict_refuses_a_ledger_that_does_not_measure_throughput(
    space_file: Path, tmp_path: Path, capsys
) -> None:
    model_id = _register_model(space_file, capsys)
    idea_id = _register_idea(space_file, capsys, model_id)
    full = tmp_path / "results.tsv"
    full.write_text(FULL_LEDGER)
    trial_id = _register_trial(space_file, full, capsys, model_id, idea_id, "win1")

    legacy = tmp_path / "legacy.tsv"
    legacy.write_text(VALUE_ONLY_LEDGER)
    assert _cli("resolve-verdict", "--space-file", space_file, "--trial-id", trial_id,
                "--ledger", legacy) == 1
    assert "throughput" in _out(capsys)


# --- FINDING 4: supervise-campaign never fabricates the ledger's throughput/diff_lines -----


def test_supervise_campaign_refuses_a_ledger_that_cannot_decide_a_verdict(
    space_file: Path, tmp_path: Path, capsys
) -> None:
    """A value-only ledger used to be accepted and its missing columns invented (throughput =
    the model's own baseline, diff_lines = 0), which makes the void check and the
    stagnant-breaches-the-net-line-bound rejection unreachable in every CLI campaign."""
    model_id = _register_model(space_file, capsys)
    _register_idea(space_file, capsys, model_id)
    legacy = tmp_path / "legacy.tsv"
    legacy.write_text(VALUE_ONLY_LEDGER)
    dispatch = _script(tmp_path, "dispatch.json", [{"commit": "lose1"}])

    assert _cli("supervise-campaign", "--space-file", space_file, "--model-id", model_id,
                "--ledger", legacy, "--dispatch-script", dispatch) == 1
    out = _out(capsys)
    assert "throughput" in out and "diff_lines" not in out.split("throughput")[0]


def test_a_campaign_trial_whose_ledger_throughput_collapsed_is_voided(
    space_file: Path, ledger: Path, tmp_path: Path, capsys
) -> None:
    """The void check reads the LEDGER's throughput for the trial's commit. With that value
    fabricated as the model's own baseline it could never fall below the 5% floor."""
    model_id = _register_model(space_file, capsys)
    _register_idea(space_file, capsys, model_id)
    dispatch = _script(tmp_path, "dispatch.json", [{"commit": "slow1"}])

    assert _cli("supervise-campaign", "--space-file", space_file, "--model-id", model_id,
                "--ledger", ledger, "--dispatch-script", dispatch, "--max-dispatches", 1) == 0
    outcome = json.loads(_out(capsys))
    assert outcome["history"][0]["status"] == "voided"


def test_a_stagnant_campaign_trial_breaching_the_net_line_bound_is_rejected_not_parked(
    space_file: Path, ledger: Path, tmp_path: Path, capsys
) -> None:
    """diff_size_limit is a real bound on this path: with diff_lines fabricated as 0 every
    stagnant trial was parked and the model's net-line bound was a no-op."""
    model_id = _register_model(space_file, capsys)
    _register_idea(space_file, capsys, model_id)
    dispatch = _script(tmp_path, "dispatch.json", [{"commit": "fat1"}])

    assert _cli("supervise-campaign", "--space-file", space_file, "--model-id", model_id,
                "--ledger", ledger, "--dispatch-script", dispatch, "--max-dispatches", 1) == 0
    outcome = json.loads(_out(capsys))
    assert outcome["history"][0]["status"] == "rejected"


def test_a_campaign_against_a_model_with_no_registered_throughput_is_refused_not_run(
    space_file: Path, ledger: Path, tmp_path: Path, capsys
) -> None:
    """A model whose harness was retired has no baseline_throughput. The old code read it with
    a bare subscript (KeyError, caught by neither handler) after defaulting a missing model to
    a fail-open 0.0 throughput floor."""
    model_id = _register_model(space_file, capsys)
    _register_idea(space_file, capsys, model_id)
    assert _cli("retire-harness", "--space-file", space_file, "--model-id", model_id,
                "--patch-json", json.dumps({"hardware": "a100"})) == 0
    capsys.readouterr()
    assert _cli("retire-harness", "--space-file", space_file, "--model-id", model_id,
                "--patch-json", json.dumps({"hardware": "h100"})) == 0
    capsys.readouterr()
    dispatch = _script(tmp_path, "dispatch.json", [{"commit": "lose1"}])

    assert _cli("supervise-campaign", "--space-file", space_file, "--model-id", model_id,
                "--ledger", ledger, "--dispatch-script", dispatch, "--max-dispatches", 1) == 1
    assert "baseline_throughput" in _out(capsys)


# --- FINDING 5: a refusal must not erase the run's durable writes -------------------------


def test_a_refusal_mid_campaign_keeps_the_trials_the_run_really_dispatched(
    space_file: Path, ledger: Path, tmp_path: Path, capsys
) -> None:
    """Two dispatches land, the third refuses. The space must show the two trials that really
    ran -- every counter the registry recomputes on resume comes from this file."""
    model_id = _register_model(space_file, capsys)
    for n in range(3):
        _register_idea(space_file, capsys, model_id, description=f"idea-{n}")
    dispatch = _script(tmp_path, "dispatch.json", [{"commit": "lose1"}, {"commit": "lose2"}])

    assert _cli("supervise-campaign", "--space-file", space_file, "--model-id", model_id,
                "--ledger", ledger, "--dispatch-script", dispatch) == 1
    assert "dispatch_script" in _out(capsys)
    assert len(_facts(space_file, "trial")) == 2


def test_a_refusal_inside_a_dispatch_keeps_the_trial_that_dispatch_registered(
    space_file: Path, ledger: Path, tmp_path: Path, capsys
) -> None:
    """The refusal here fires DURING the second dispatch (its self-reported throughput
    disagrees with the ledger), after that trial was already registered. The load/mutate/save
    sequence used to drop the whole run's writes on any refusal, so the space showed neither
    trial -- and the disagreement it refused on left no record at all."""
    model_id = _register_model(space_file, capsys)
    for n in range(2):
        _register_idea(space_file, capsys, model_id, description=f"idea-{n}")
    dispatch = _script(
        tmp_path, "dispatch.json",
        [{"commit": "lose1"}, {"commit": "lose2", "throughput": 999}],
    )

    assert _cli("supervise-campaign", "--space-file", space_file, "--model-id", model_id,
                "--ledger", ledger, "--dispatch-script", dispatch) == 1
    assert "throughput" in _out(capsys)
    assert len(_facts(space_file, "trial")) == 2


# --- FINDING 12: the citation resolution outcome is not a caller input --------------------


def test_resolve_citation_refuses_to_take_the_resolution_outcome_from_its_caller(
    space_file: Path, capsys
) -> None:
    model_id = _register_model(space_file, capsys)
    idea_id = _register_idea(space_file, capsys, model_id)
    assert _cli("resolve-citation", "--space-file", space_file, "--idea-id", idea_id,
                "--reference", "arxiv:2401.99999", "--outcome", "resolved",
                "--title", "Anything") == 1
    assert "resolver" in _out(capsys)
    idea = next(f for f in _facts(space_file, "idea") if f["id"] == idea_id)
    assert "basis" not in idea["meta"]


def test_a_resolved_outcome_requires_a_title_and_an_author(space_file: Path, capsys) -> None:
    model_id = _register_model(space_file, capsys)
    idea_id = _register_idea(space_file, capsys, model_id)
    assert _cli("resolve-citation", "--test-resolver", "--space-file", space_file,
                "--idea-id", idea_id, "--reference", "arxiv:2401.99999", "--outcome", "resolved") == 1
    assert "title" in _out(capsys)
    assert _cli("resolve-citation", "--test-resolver", "--space-file", space_file,
                "--idea-id", idea_id, "--reference", "arxiv:2401.99999", "--outcome", "resolved",
                "--title", "Attention Is All You Need") == 1
    assert "authors" in _out(capsys)


def test_the_test_resolver_still_drives_the_real_write_path(space_file: Path, capsys) -> None:
    model_id = _register_model(space_file, capsys)
    idea_id = _register_idea(space_file, capsys, model_id)
    assert _cli("resolve-citation", "--test-resolver", "--space-file", space_file,
                "--idea-id", idea_id, "--reference", "arxiv:2401.99999", "--outcome", "resolved",
                "--title", "Attention Is All You Need", "--author", "Vaswani") == 0
    idea = next(f for f in _facts(space_file, "idea") if f["id"] == idea_id)
    assert idea["meta"]["basis"] == "external"


# --- FINDING 15: updating a registered model is a guarded, merging mutation ---------------


def test_updating_a_registered_model_cannot_move_the_baseline_from_a_worker(
    space_file: Path, capsys
) -> None:
    model_id = _register_model(space_file, capsys)
    assert _cli("register-model", "--space-file", space_file, "--model-id", model_id,
                "--source", "worker",
                "--meta-json", json.dumps({"baseline": "win1"})) == 1
    assert "baseline" in _out(capsys)
    model = next(f for f in _facts(space_file, "model") if f["id"] == model_id)
    assert model["meta"]["baseline"] == "base"


def test_updating_a_registered_model_cannot_widen_the_noise_floor_from_a_worker(
    space_file: Path, capsys
) -> None:
    model_id = _register_model(space_file, capsys)
    assert _cli("register-model", "--space-file", space_file, "--model-id", model_id,
                "--source", "worker",
                "--meta-json", json.dumps({"noise_floor": 999.0})) == 1
    assert "noise_floor" in _out(capsys)
    model = next(f for f in _facts(space_file, "model") if f["id"] == model_id)
    assert model["meta"]["noise_floor"] == 0.01


def test_updating_a_registered_model_keeps_the_derived_campaign_state(
    space_file: Path, ledger: Path, tmp_path: Path, capsys
) -> None:
    """A wholesale replace dropped campaign_status/ratchet_count/rejection_streak_ideas and
    the keep-pushing markers -- exactly the state guarantee 4 says must be recomputed from the
    registry rather than held in session state."""
    model_id = _register_model(space_file, capsys)
    _register_idea(space_file, capsys, model_id)
    dispatch = _script(tmp_path, "dispatch.json", [{"commit": "lose1"}])
    assert _cli("supervise-campaign", "--space-file", space_file, "--model-id", model_id,
                "--ledger", ledger, "--dispatch-script", dispatch) == 0
    capsys.readouterr()
    assert _cli("record-keep-pushing-marker", "--space-file", space_file, "--model-id", model_id,
                "--axis", "architecture", "--author", "matt") == 0
    capsys.readouterr()

    assert _cli("register-model", "--space-file", space_file, "--model-id", model_id,
                "--source", "adjudication",
                "--meta-json", json.dumps({"max_trials": 12})) == 0
    model = next(f for f in _facts(space_file, "model") if f["id"] == model_id)
    assert model["meta"]["max_trials"] == 12
    assert model["meta"]["campaign_status"] == "completed"
    assert model["meta"]["rejection_streak_ideas"]
    assert model["meta"]["keep_pushing_markers"]["architecture"]["author"] == "matt"


def test_updating_a_registered_model_without_a_source_is_refused_naming_it(
    space_file: Path, capsys
) -> None:
    model_id = _register_model(space_file, capsys)
    assert _cli("register-model", "--space-file", space_file, "--model-id", model_id,
                "--meta-json", json.dumps({"max_trials": 12})) == 1
    assert "source" in _out(capsys)


def test_a_registered_models_metric_stays_frozen_on_the_update_path(
    space_file: Path, capsys
) -> None:
    model_id = _register_model(space_file, capsys)
    assert _cli("register-model", "--space-file", space_file, "--model-id", model_id,
                "--source", "adjudication",
                "--meta-json", json.dumps({"metric": "perplexity"})) == 1
    assert "metric" in _out(capsys)


# --- FINDING 16: a budget is never silently unlimited -------------------------------------


@pytest.mark.parametrize("budget_field", ["max_discovered_ideas", "max_trials", "per_trial_seconds"])
def test_an_explicitly_null_budget_takes_the_documented_default(
    space_file: Path, capsys, budget_field: str
) -> None:
    """``setdefault`` fills only a MISSING key, so an explicit null survived: for
    max_discovered_ideas it became the unlimited sentinel, for max_trials a TypeError."""
    defaults = {"max_discovered_ideas": 8, "max_trials": 200, "per_trial_seconds": 420}
    model_id = _register_model(space_file, capsys, **{budget_field: None})
    model = next(f for f in _facts(space_file, "model") if f["id"] == model_id)
    assert model["meta"][budget_field] == defaults[budget_field]


@pytest.mark.parametrize(
    "budget",
    [
        {"max_discovered_ideas": -5},
        {"max_discovered_ideas": "lots"},
        {"max_trials": 0},
        {"max_trials": "many"},
        {"per_trial_seconds": -1},
    ],
)
def test_an_unusable_budget_is_a_named_refusal_never_unlimited(
    space_file: Path, capsys, budget: dict
) -> None:
    assert _cli("register-model", "--space-file", space_file,
                "--meta-json", json.dumps({**MODEL_META, **budget})) == 1
    assert next(iter(budget)) in _out(capsys)


def test_unlimited_discovered_ideas_stays_reachable_by_its_explicit_sentinel(
    space_file: Path, capsys
) -> None:
    model_id = _register_model(space_file, capsys, max_discovered_ideas=-1)
    model = next(f for f in _facts(space_file, "model") if f["id"] == model_id)
    assert model["meta"]["max_discovered_ideas"] == -1


def test_updating_a_model_never_resets_a_budget_it_did_not_mention(
    space_file: Path, capsys
) -> None:
    model_id = _register_model(space_file, capsys, max_trials=7)
    assert _cli("register-model", "--space-file", space_file, "--model-id", model_id,
                "--source", "adjudication", "--meta-json", json.dumps({"diff_size_limit": 900})) == 0
    model = next(f for f in _facts(space_file, "model") if f["id"] == model_id)
    assert model["meta"]["max_trials"] == 7


# --- FINDING 18: the R9 marker and the R17 filing path are reachable from the CLI ---------


def test_the_only_rabbit_hole_suppression_is_authorable_from_the_cli(
    space_file: Path, capsys
) -> None:
    model_id = _register_model(space_file, capsys)
    assert _cli("record-keep-pushing-marker", "--space-file", space_file, "--model-id", model_id,
                "--axis", "architecture", "--author", "matt") == 0
    model = next(f for f in _facts(space_file, "model") if f["id"] == model_id)
    assert model["meta"]["keep_pushing_markers"] == {"architecture": {"author": "matt"}}


def test_an_out_of_diff_change_is_authorable_from_the_cli_and_needs_an_author(
    space_file: Path, capsys
) -> None:
    model_id = _register_model(space_file, capsys)
    assert _cli("record-out-of-diff-change", "--space-file", space_file, "--model-id", model_id,
                "--author", "matt") == 0
    model = next(f for f in _facts(space_file, "model") if f["id"] == model_id)
    assert model["meta"]["out_of_diff_changes"] == [{"before_trial_index": 0, "author": "matt"}]
    assert _cli("record-out-of-diff-change", "--space-file", space_file, "--model-id", model_id,
                "--author", " ") == 1
    assert "author" in _out(capsys)


def test_a_confirmed_cross_model_lesson_is_actually_filed_from_the_cli(
    space_file: Path, ledger: Path, tmp_path: Path, capsys
) -> None:
    """R17's filing seam was never wired here, so a confirmed cross-model insight died
    in-process and guarantee 8 was unreachable from the shipped entrypoint."""
    prior_model = _register_model(space_file, capsys)
    prior_idea = _register_idea(space_file, capsys, prior_model)
    _register_trial(space_file, ledger, capsys, prior_model, prior_idea, "lose1")
    assert _cli("reject-idea", "--space-file", space_file, "--idea-id", prior_idea,
                "--reason", "fell below baseline") == 0
    capsys.readouterr()

    model_id = _register_model(space_file, capsys)
    _register_idea(space_file, capsys, model_id)  # the SAME (axis, description) insight
    dispatch = _script(tmp_path, "dispatch.json", [{"commit": "lose2"}])
    lesson_file = tmp_path / "lessons.jsonl"

    assert _cli("supervise-campaign", "--space-file", space_file, "--model-id", model_id,
                "--ledger", ledger, "--dispatch-script", dispatch,
                "--lesson-file", lesson_file) == 0
    outcome = json.loads(_out(capsys))
    assert len(outcome["lessons_filed"]) == 1
    filed = [json.loads(line) for line in lesson_file.read_text().splitlines()]
    assert len(filed) == 1
    assert filed[0]["meta"]["insight_key"] == "architecture::try rope"
    assert sorted(filed[0]["meta"]["model_trial_counts"]) == sorted([prior_model, model_id])
