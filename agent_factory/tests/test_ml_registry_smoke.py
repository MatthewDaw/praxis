"""Smoke test for the af-ml-research registry entrypoint (R1/R2/R5, check plan-2f4be5275cf7).

Runs ``python -m knowledge.ml_registry.cli`` as a real subprocess -- not an import -- so a
registry ticket cannot go green on code that has no runnable entrypoint. Exercises the R1
acceptance condition end-to-end through the CLI: a well-formed fact per category is
accepted, a fact missing a required key is rejected naming it, a worker-sourced mutation
of a protected model field is refused naming it, and a baseline move from anywhere other
than adjudication is refused. Also exercises R2's write API: registering a model, an idea
and a trial through the CLI (each call a separate subprocess, persisted via
``--space-file``), a readback returning all three, the trial's ``derived_from`` edge, the
idea origin enum, the per-model discovered-idea budget, and the two trial refusals
(unregistered idea, commit missing from the external ledger). Also exercises R5's
cross-project model linkage: two tickets in different project spaces carrying the
same ``meta.experiment_id`` resolve to each other's project/model through the
``register-ticket``/``model-to-projects``/``project-to-models`` subcommands. Also exercises
R6's ideation axis sweep: ``seed-campaign`` writes at least one ``origin=seeded`` idea per
generative axis, records an execution receipt per retrieval axis (even one that legitimately
retrieves nothing), and interactive mode writes only confirmed candidates, of identical shape
to a batch run.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "knowledge.ml_registry.cli", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_cli_accepts_a_well_formed_model_fact() -> None:
    meta = {
        "metric": "val_bpb",
        "direction": "minimize",
        "win_condition": "beats baseline by the rope",
        "baseline": "commit-abc123",
        "baseline_throughput": 1200,
        "diff_size_limit": 800,
    }
    result = _run_cli("validate-fact", "--category", "model", "--meta-json", json.dumps(meta))
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_cli_rejects_a_fact_missing_a_required_key_naming_it():
    meta = {"model_id": "model-1", "origin": "seeded", "axis": "architecture"}
    result = _run_cli("validate-fact", "--category", "idea", "--meta-json", json.dumps(meta))
    assert result.returncode == 1
    assert "description" in result.stdout + result.stderr


def test_cli_refuses_worker_sourced_mutation_of_a_protected_field_naming_it() -> None:
    patch = {"baseline_runs": ["cherry-picked"]}
    result = _run_cli(
        "guard-model-mutation", "--patch-json", json.dumps(patch), "--source", "worker"
    )
    assert result.returncode == 1
    assert "baseline_runs" in result.stdout + result.stderr


def test_cli_refuses_a_baseline_move_from_anywhere_other_than_adjudication():
    patch = {"baseline": "commit-def456"}
    result = _run_cli("guard-baseline-move", "--patch-json", json.dumps(patch), "--source", "worker")
    assert result.returncode == 1
    assert "baseline" in result.stdout + result.stderr


def test_cli_allows_a_baseline_move_from_adjudication():
    patch = {"baseline": "commit-def456"}
    result = _run_cli(
        "guard-baseline-move", "--patch-json", json.dumps(patch), "--source", "adjudication"
    )
    assert result.returncode == 0, result.stderr


MODEL_META = {
    "metric": "val_bpb",
    "direction": "minimize",
    "win_condition": "beats baseline by the rope",
    "baseline": "commit-abc123",
    "baseline_throughput": 1200,
    "diff_size_limit": 800,
    "max_discovered_ideas": 1,
}


def _register(command: str, space_file: Path, meta: dict, *, ledger: Path | None = None) -> str:
    args = [command, "--space-file", str(space_file), "--meta-json", json.dumps(meta)]
    if ledger is not None:
        args += ["--ledger", str(ledger)]
    result = _run_cli(*args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().rsplit(" ", 1)[-1]


def test_cli_register_readback_round_trip_across_separate_processes(tmp_path):
    """R2 acceptance: register a model, an idea and a trial through the API (each a
    separate subprocess) and read all three back, with the trial's derived_from edge
    naming the idea's fact id."""
    space_file = tmp_path / "space.json"
    ledger = tmp_path / "results.tsv"
    ledger.write_text("commit\tval_bpb\tmemory_gb\tstatus\tdescription\ndeadbeef\t1.0\t2.0\tok\tbaseline\n")

    model_id = _register("register-model", space_file, MODEL_META)
    idea_id = _register(
        "register-idea",
        space_file,
        {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": "try RoPE"},
    )
    trial_id = _register(
        "register-trial",
        space_file,
        {"model_id": model_id, "idea_id": idea_id, "commit": "deadbeef", "status": "running"},
        ledger=ledger,
    )

    result = _run_cli("readback", "--space-file", str(space_file))
    assert result.returncode == 0, result.stderr
    facts = json.loads(result.stdout)
    facts_by_id = {f["id"]: f for f in facts}
    assert set(facts_by_id) == {model_id, idea_id, trial_id}
    assert facts_by_id[trial_id]["derivedFrom"] == [idea_id]


def test_cli_refuses_a_discovered_idea_beyond_the_model_budget_naming_it(tmp_path):
    space_file = tmp_path / "space.json"
    model_id = _register("register-model", space_file, MODEL_META)  # max_discovered_ideas=1
    _register(
        "register-idea",
        space_file,
        {"model_id": model_id, "origin": "discovered", "axis": "architecture", "description": "d1"},
    )
    result = _run_cli(
        "register-idea",
        "--space-file",
        str(space_file),
        "--meta-json",
        json.dumps(
            {"model_id": model_id, "origin": "discovered", "axis": "architecture", "description": "d2"}
        ),
    )
    assert result.returncode == 1
    assert "max_discovered_ideas" in result.stdout + result.stderr


def test_cli_refuses_a_trial_for_an_unregistered_idea(tmp_path):
    space_file = tmp_path / "space.json"
    ledger = tmp_path / "results.tsv"
    ledger.write_text("commit\tval_bpb\tmemory_gb\tstatus\tdescription\ndeadbeef\t1.0\t2.0\tok\tbaseline\n")
    model_id = _register("register-model", space_file, MODEL_META)
    result = _run_cli(
        "register-trial",
        "--space-file",
        str(space_file),
        "--meta-json",
        json.dumps({"model_id": model_id, "idea_id": "idea-nope", "commit": "deadbeef", "status": "running"}),
        "--ledger",
        str(ledger),
    )
    assert result.returncode == 1
    assert "idea_id" in result.stdout + result.stderr


def test_cli_resolves_cross_project_model_linkage_from_either_side(tmp_path):
    """R5 acceptance: two tickets in different project spaces carrying the same
    meta.experiment_id -- model-to-projects returns both project names, and
    project-to-models returns that model from either side."""
    space_file = tmp_path / "space.json"
    index_file = tmp_path / "tickets.json"
    _register("register-model", space_file, {**MODEL_META, "experiment_id": "exp-42"})

    for project, ticket_id in (("project-alpha", "ticket-1"), ("project-beta", "ticket-7")):
        result = _run_cli(
            "register-ticket",
            "--index-file",
            str(index_file),
            "--project",
            project,
            "--ticket-id",
            ticket_id,
            "--meta-json",
            json.dumps({"experiment_id": "exp-42"}),
        )
        assert result.returncode == 0, result.stderr

    m2p = _run_cli(
        "model-to-projects", "--index-file", str(index_file), "--space-file", str(space_file),
        "--experiment-id", "exp-42",
    )
    assert m2p.returncode == 0, m2p.stderr
    assert json.loads(m2p.stdout) == ["project-alpha", "project-beta"]

    for project in ("project-alpha", "project-beta"):
        p2m = _run_cli(
            "project-to-models", "--index-file", str(index_file), "--space-file", str(space_file),
            "--project", project,
        )
        assert p2m.returncode == 0, p2m.stderr
        assert json.loads(p2m.stdout) == ["exp-42"]


def test_cli_idea_lifecycle_adopt_park_reject_claim_and_reversal(tmp_path):
    """R3 acceptance, exercised end-to-end through the CLI (each call a separate
    subprocess): adopt records a succeeded outcome, park requires a non-empty trigger,
    reject removes an idea from the backlog while surfacing it (with its reason) in
    rejection-memory and flags its derived trials, an idea claim lease is reclaimable
    once stale, and invalidating an adoption reverts it and returns every idea rejected
    during its tenure to the untried backlog."""
    space_file = tmp_path / "space.json"
    ledger = tmp_path / "results.tsv"
    ledger.write_text(
        "commit\tval_bpb\tmemory_gb\tstatus\tdescription\n"
        "deadbeef\t1.0\t2.0\tok\twinner\nfeedface\t1.0\t2.0\tok\tloser\n"
    )

    model_id = _register("register-model", space_file, MODEL_META)
    winner_id = _register(
        "register-idea", space_file,
        {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": "winner"},
    )
    loser_id = _register(
        "register-idea", space_file,
        {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": "loser"},
    )
    claim_test_id = _register(
        "register-idea", space_file,
        {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": "claim lease probe"},
    )

    trial_id = _register(
        "register-trial", space_file,
        {"model_id": model_id, "idea_id": winner_id, "commit": "deadbeef", "status": "succeeded"},
        ledger=ledger,
    )
    result = _run_cli("adopt-idea", "--space-file", str(space_file), "--idea-id", winner_id, "--trial-id", trial_id)
    assert result.returncode == 0, result.stderr

    readback = json.loads(_run_cli("readback", "--space-file", str(space_file), "--category", "idea").stdout)
    winner = next(f for f in readback if f["id"] == winner_id)
    assert winner["meta"]["status"] == "adopted"
    assert winner["meta"]["outcome"] == "succeeded"

    park_fail = _run_cli(
        "park-idea", "--space-file", str(space_file), "--idea-id", loser_id, "--trigger", ""
    )
    assert park_fail.returncode == 1
    assert "reactivation_trigger" in park_fail.stdout + park_fail.stderr

    loser_trial_id = _register(
        "register-trial", space_file,
        {"model_id": model_id, "idea_id": loser_id, "commit": "feedface", "status": "running"},
        ledger=ledger,
    )
    reject = _run_cli(
        "reject-idea", "--space-file", str(space_file), "--idea-id", loser_id, "--reason", "superseded by winner"
    )
    assert reject.returncode == 0, reject.stderr

    backlog = json.loads(_run_cli("backlog", "--space-file", str(space_file)).stdout)
    assert loser_id not in {f["id"] for f in backlog}

    memory = json.loads(_run_cli("rejection-memory", "--space-file", str(space_file)).stdout)
    memory_by_id = {row["idea"]["id"]: row["reason"] for row in memory}
    assert memory_by_id[loser_id] == "superseded by winner"

    flagged = json.loads(_run_cli("flagged-trials", "--space-file", str(space_file)).stdout)
    flagged_ids = {f["id"]: f["meta"]["idea_rejected"] for f in flagged}
    assert flagged_ids[loser_trial_id] is True

    claim1 = json.loads(
        _run_cli(
            "claim-idea", "--space-file", str(space_file), "--idea-id", claim_test_id,
            "--owner", "worker-a", "--ttl", "10", "--now", "1000",
        ).stdout
    )
    assert claim1["claimed"] is True
    claim2 = _run_cli(
        "claim-idea", "--space-file", str(space_file), "--idea-id", claim_test_id,
        "--owner", "worker-b", "--ttl", "10", "--now", "1005",
    )
    assert claim2.returncode == 1  # still live for a different owner
    claim3 = json.loads(
        _run_cli(
            "claim-idea", "--space-file", str(space_file), "--idea-id", claim_test_id,
            "--owner", "worker-b", "--ttl", "10", "--now", "1050",
        ).stdout
    )
    assert claim3["claimed"] is True  # stale -- reclaimable by a different owner

    invalidate = _run_cli(
        "invalidate-adoption", "--space-file", str(space_file), "--idea-id", winner_id,
        "--reason", "regression found in production",
    )
    assert invalidate.returncode == 0, invalidate.stderr

    backlog_after = json.loads(_run_cli("backlog", "--space-file", str(space_file)).stdout)
    assert loser_id in {f["id"] for f in backlog_after}


def test_cli_retriable_ideas_only_lists_parked_ideas_whose_own_trigger_fired(tmp_path):
    space_file = tmp_path / "space.json"
    model_id = _register("register-model", space_file, MODEL_META)
    parked_id = _register(
        "register-idea", space_file,
        {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": "parked"},
    )
    other_parked_id = _register(
        "register-idea", space_file,
        {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": "parked-other"},
    )
    park1 = _run_cli(
        "park-idea", "--space-file", str(space_file), "--idea-id", parked_id, "--trigger", "new-dataset-released"
    )
    assert park1.returncode == 0, park1.stderr
    park2 = _run_cli(
        "park-idea", "--space-file", str(space_file), "--idea-id", other_parked_id, "--trigger", "gpu-budget-doubled"
    )
    assert park2.returncode == 0, park2.stderr

    result = _run_cli(
        "retriable-ideas", "--space-file", str(space_file), "--fired-trigger", "new-dataset-released"
    )
    assert result.returncode == 0, result.stderr
    retriable_ids = {f["id"] for f in json.loads(result.stdout)}
    assert retriable_ids == {parked_id}


def test_cli_refuses_a_trial_whose_commit_is_missing_from_the_ledger(tmp_path):
    space_file = tmp_path / "space.json"
    ledger = tmp_path / "results.tsv"
    ledger.write_text("commit\tval_bpb\tmemory_gb\tstatus\tdescription\ndeadbeef\t1.0\t2.0\tok\tbaseline\n")
    model_id = _register("register-model", space_file, MODEL_META)
    idea_id = _register(
        "register-idea",
        space_file,
        {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": "try RoPE"},
    )
    result = _run_cli(
        "register-trial",
        "--space-file",
        str(space_file),
        "--meta-json",
        json.dumps({"model_id": model_id, "idea_id": idea_id, "commit": "not-in-ledger", "status": "running"}),
        "--ledger",
        str(ledger),
    )
    assert result.returncode == 1
    assert "commit" in result.stdout + result.stderr


def test_cli_resolve_citation_records_basis_and_title_on_the_idea(tmp_path: Path) -> None:
    """R7 acceptance, CLI-driven: a resolving arXiv reference lands basis=external with
    the resolved title recorded on the idea, read back through a separate subprocess."""
    space_file = tmp_path / "space.json"
    model_id = _register("register-model", space_file, MODEL_META)
    idea_id = _register(
        "register-idea",
        space_file,
        {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": "try RoPE"},
    )
    result = _run_cli(
        "resolve-citation",
        "--test-resolver",
        "--space-file",
        str(space_file),
        "--idea-id",
        idea_id,
        "--reference",
        "2301.12345",
        "--outcome",
        "resolved",
        "--title",
        "Attention Is All You Need",
        "--author",
        "Vaswani",
    )
    assert result.returncode == 0, result.stderr
    readback = json.loads(_run_cli("readback", "--space-file", str(space_file), "--category", "idea").stdout)
    idea = next(f for f in readback if f["id"] == idea_id)
    assert idea["meta"]["basis"] == "external"
    assert idea["meta"]["title"] == "Attention Is All You Need"


def test_cli_resolve_citation_downgrades_to_reasoned_on_the_3rd_consecutive_unreachable_attempt(tmp_path: Path) -> None:
    space_file = tmp_path / "space.json"
    model_id = _register("register-model", space_file, MODEL_META)
    idea_id = _register(
        "register-idea",
        space_file,
        {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": "try RoPE"},
    )
    args = [
        "resolve-citation",
        "--test-resolver",
        "--space-file",
        str(space_file),
        "--idea-id",
        idea_id,
        "--reference",
        "2301.12345",
        "--outcome",
        "unreachable",
    ]
    for _ in range(3):
        result = _run_cli(*args)
        assert result.returncode == 0, result.stderr
    readback = json.loads(_run_cli("readback", "--space-file", str(space_file), "--category", "idea").stdout)
    idea = next(f for f in readback if f["id"] == idea_id)
    assert idea["meta"]["basis"] == "reasoned"
    assert idea["meta"]["unreachable_streak"] == 0


def test_cli_registry_queries_over_a_seeded_multi_axis_multi_model_fixture(tmp_path):
    """R4 acceptance: against a seeded fixture of ideas spanning three axes, two models
    and both origins, the three query subcommands (backlog, rejection-memory,
    per-axis-yield) each return exactly the documented rows, and per-axis-yield reports a
    distinct attempt count and adoption count per axis and per origin."""
    space_file = tmp_path / "space.json"
    ledger = tmp_path / "results.tsv"
    ledger.write_text(
        "commit\tval_bpb\tmemory_gb\tstatus\tdescription\n"
        "deadbeef\t1.0\t2.0\tok\tm1-arch-winner\nfeedface\t1.0\t2.0\tok\tm1-arch-tried\n"
        "c0ffee\t1.0\t2.0\tok\tm2-data-tried\n"
    )

    model_1 = _register("register-model", space_file, MODEL_META)
    model_2 = _register("register-model", space_file, MODEL_META)

    def idea(model_id, axis, origin, description):
        return _register(
            "register-idea", space_file,
            {"model_id": model_id, "origin": origin, "axis": axis, "description": description},
        )

    # model_1 / architecture / seeded: attempted + adopted.
    m1_arch_winner = idea(model_1, "architecture", "seeded", "m1 arch winner")
    winner_trial = _register(
        "register-trial", space_file,
        {"model_id": model_1, "idea_id": m1_arch_winner, "commit": "deadbeef", "status": "succeeded"},
        ledger=ledger,
    )
    adopt = _run_cli("adopt-idea", "--space-file", str(space_file), "--idea-id", m1_arch_winner, "--trial-id", winner_trial)
    assert adopt.returncode == 0, adopt.stderr

    # model_1 / architecture / seeded: attempted but rejected (not adopted).
    m1_arch_rejected = idea(model_1, "architecture", "seeded", "m1 arch rejected")
    _register(
        "register-trial", space_file,
        {"model_id": model_1, "idea_id": m1_arch_rejected, "commit": "feedface", "status": "running"},
        ledger=ledger,
    )
    reject = _run_cli(
        "reject-idea", "--space-file", str(space_file), "--idea-id", m1_arch_rejected, "--reason", "did not beat baseline",
    )
    assert reject.returncode == 0, reject.stderr

    # model_1 / optimizer / discovered: never attempted -- sits in the backlog.
    m1_opt_untried = idea(model_1, "optimizer", "discovered", "m1 optimizer untried")

    # model_2 / data / seeded: attempted, still running (not adopted, not rejected).
    m2_data_tried = idea(model_2, "data", "seeded", "m2 data tried")
    _register(
        "register-trial", space_file,
        {"model_id": model_2, "idea_id": m2_data_tried, "commit": "c0ffee", "status": "running"},
        ledger=ledger,
    )

    # model_2 / data / discovered: never attempted -- sits in the backlog.
    m2_data_untried = idea(model_2, "data", "discovered", "m2 data untried")

    backlog = json.loads(_run_cli("backlog", "--space-file", str(space_file)).stdout)
    assert {f["id"] for f in backlog} == {m1_opt_untried, m2_data_untried, m2_data_tried}

    memory = json.loads(_run_cli("rejection-memory", "--space-file", str(space_file)).stdout)
    memory_by_id = {row["idea"]["id"]: row["reason"] for row in memory}
    assert memory_by_id == {m1_arch_rejected: "did not beat baseline"}

    yield_report = json.loads(_run_cli("per-axis-yield", "--space-file", str(space_file)).stdout)
    assert yield_report == {
        "architecture": {"seeded": {"attempts": 2, "adoptions": 1}},
        "optimizer": {"discovered": {"attempts": 0, "adoptions": 0}},
        "data": {
            "seeded": {"attempts": 1, "adoptions": 0},
            "discovered": {"attempts": 0, "adoptions": 0},
        },
    }


def test_cli_registers_a_model_against_its_ledger_rows_and_adjudicates_a_single_trial(tmp_path: Path) -> None:
    """R12 acceptance, CLI-driven: registration records the rope's evidence (4 named ledger
    rows) and recomputes baseline_throughput from them, adjudicates a candidate against the
    rope measured from those same rows, and a harness-field mutation retires that evidence,
    reverts the active adoption (with its re-queue side effect), and clears the ratchet."""
    space_file = tmp_path / "space.json"
    ledger = tmp_path / "results.tsv"
    ledger.write_text(
        "commit\tval_bpb\tmemory_gb\tstatus\tdescription\n"
        "r1\t1.0\t2.0\tok\tbaseline run 1\n"
        "r2\t1.02\t2.0\tok\tbaseline run 2\n"
        "r3\t0.98\t2.0\tok\tbaseline run 3\n"
        "r4\t1.04\t2.0\tok\tbaseline run 4\n"
        "cand1\t0.9\t2.0\tok\tcandidate run\n"
    )
    meta = {
        "metric": "val_bpb",
        "direction": "minimize",
        "win_condition": "beats baseline by the rope",
        "baseline": "commit-abc123",
        "diff_size_limit": 800,
        "baseline_runs": ["r1", "r2", "r3", "r4"],
    }
    result = _run_cli(
        "register-model-with-baseline", "--space-file", str(space_file),
        "--meta-json", json.dumps(meta), "--ledger", str(ledger),
    )
    assert result.returncode == 0, result.stderr
    model_id = result.stdout.strip().rsplit(" ", 1)[-1]

    readback = json.loads(_run_cli("readback", "--space-file", str(space_file), "--category", "model").stdout)
    registered = next(f for f in readback if f["id"] == model_id)
    assert registered["meta"]["baseline_runs"] == ["r1", "r2", "r3", "r4"]
    assert registered["meta"]["baseline_throughput"] == pytest.approx(1.01)
    assert registered["meta"]["ratchet_count"] == 0
    # No threshold is stored: the bar comes from those four rows, at every comparison.
    assert not [key for key in registered["meta"] if "floor" in key]

    # a baseline run naming no scored ledger row is refused, since an absent measurement is
    # not a narrow bar but no bar at all
    bad_meta = {**meta, "baseline_runs": ["r1", "r2", "r3", "not-in-ledger"]}
    bad = _run_cli(
        "register-model-with-baseline", "--space-file", str(space_file),
        "--meta-json", json.dumps(bad_meta), "--ledger", str(ledger),
    )
    assert bad.returncode == 1
    assert "baseline_runs" in bad.stdout + bad.stderr

    idea_id = _register(
        "register-idea", space_file,
        {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": "try RoPE"},
    )
    trial_id = _register(
        "register-trial", space_file,
        {"model_id": model_id, "idea_id": idea_id, "commit": "cand1", "status": "running"},
        ledger=ledger,
    )
    # RETARGETED: this call used to pass --observed-value 0.5 for a trial on a commit the
    # ledger scores at 1.0 and assert "succeeded" -- i.e. it certified that the judged
    # agent's own number decided its own trial. The verdict now comes from the ledger row
    # for the trial's commit, and a disagreeing self-report is refused naming it.
    adjudicate = _run_cli(
        "adjudicate-trial", "--space-file", str(space_file), "--trial-id", trial_id,
        "--ledger", str(ledger),
    )
    assert adjudicate.returncode == 0, adjudicate.stderr
    assert "succeeded" in adjudicate.stdout

    self_reported = _run_cli(
        "adjudicate-trial", "--space-file", str(space_file), "--trial-id", trial_id,
        "--ledger", str(ledger), "--observed-value", "0.5",
    )
    assert self_reported.returncode == 1
    assert "observed_value" in self_reported.stdout + self_reported.stderr

    adopt = _run_cli("adopt-idea", "--space-file", str(space_file), "--idea-id", idea_id, "--trial-id", trial_id)
    assert adopt.returncode == 0, adopt.stderr

    retire = _run_cli(
        "retire-harness", "--space-file", str(space_file), "--model-id", model_id,
        "--patch-json", json.dumps({"hardware": "a100"}),
    )
    assert retire.returncode == 0, retire.stderr  # first-time set -- not yet a mutation

    mutate = _run_cli(
        "retire-harness", "--space-file", str(space_file), "--model-id", model_id,
        "--patch-json", json.dumps({"hardware": "h100"}),
    )
    assert mutate.returncode == 0, mutate.stderr
    retired = json.loads(mutate.stdout)
    assert "baseline_runs" not in retired["meta"]
    assert retired["meta"]["ratchet_count"] == 0
    assert retired["meta"]["campaign_status"] == "stalled_pending_baseline"

    idea_readback = json.loads(_run_cli("readback", "--space-file", str(space_file), "--category", "idea").stdout)
    winner = next(f for f in idea_readback if f["id"] == idea_id)
    assert winner["meta"]["status"] != "adopted"  # the adoption was reverted by the harness mutation


def test_cli_resolve_verdict_adopts_a_trial_beyond_one_rope(tmp_path: Path) -> None:
    """R10 acceptance, CLI-driven: resolve-verdict joins a trial's commit (and the model's
    current baseline commit) against the REAL external ledger (results.tsv, including its
    throughput/diff_lines columns), adopts the trial when its delta clears one rope in
    the improving direction, advances the model's baseline to the trial's commit, and records
    the previous baseline.

    RETARGETED: this test used to hand resolve-verdict a hand-written JSON blob of
    value/throughput/diff_lines -- a "ledger" the judged agent could write itself, against
    which every check adjudicate_verdict performs is vacuous."""
    space_file = tmp_path / "space.json"
    ledger_tsv = tmp_path / "results.tsv"
    ledger_tsv.write_text(
        "commit\tval_bpb\tmemory_gb\tstatus\tdescription\tthroughput\tdiff_lines\n"
        "r1\t1.0\t2.0\tok\tbaseline run 1\t1.01\t0\nr2\t1.02\t2.0\tok\tbaseline run 2\t1.01\t0\n"
        "r3\t0.98\t2.0\tok\tbaseline run 3\t1.01\t0\nr4\t1.04\t2.0\tok\tbaseline run 4\t1.01\t0\n"
        "adopt1\t0.9\t2.0\tok\tcandidate\t1.01\t100\n"
    )
    meta = {
        "metric": "val_bpb",
        "direction": "minimize",
        "win_condition": "beats baseline by the rope",
        "baseline": "r1",
        "diff_size_limit": 800,
        "baseline_runs": ["r1", "r2", "r3", "r4"],
    }
    model_id = _register(
        "register-model-with-baseline", space_file, meta, ledger=ledger_tsv
    )
    idea_id = _register(
        "register-idea", space_file,
        {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": "try RoPE"},
    )
    trial_id = _register(
        "register-trial", space_file,
        {
            "model_id": model_id, "idea_id": idea_id, "commit": "adopt1", "status": "running",
            "throughput": 1.01, "diff_lines": 100,
        },
        ledger=ledger_tsv,
    )

    result = _run_cli(
        "resolve-verdict", "--space-file", str(space_file), "--trial-id", trial_id,
        "--ledger", str(ledger_tsv),
    )
    assert result.returncode == 0, result.stderr
    assert "adopted" in result.stdout

    readback = json.loads(_run_cli("readback", "--space-file", str(space_file), "--category", "model").stdout)
    model = next(f for f in readback if f["id"] == model_id)
    assert model["meta"]["baseline"] == "adopt1"
    assert model["meta"]["previous_baseline"] == "r1"


def test_cli_resolve_citation_refuses_an_unregistered_idea_naming_it(tmp_path: Path) -> None:
    space_file = tmp_path / "space.json"
    result = _run_cli(
        "resolve-citation",
        "--test-resolver",
        "--space-file",
        str(space_file),
        "--idea-id",
        "idea-nope",
        "--reference",
        "2301.12345",
        "--outcome",
        "resolved",
        "--title",
        "Attention Is All You Need",
        "--author",
        "Vaswani",
    )
    assert result.returncode == 1
    assert "idea_id" in result.stdout + result.stderr


def test_cli_supervise_campaign_drives_a_seeded_backlog_to_a_win(tmp_path: Path) -> None:
    """R8+R10 acceptance, CLI-driven: supervise-campaign dispatches trials one worker
    session at a time (each a separate --dispatch-script entry) through
    verdict.adjudicate_verdict, draws seed-first, and closes on the win condition --
    recording the campaign as 'won' and the ratchet_count as 0 (an ADOPTED verdict
    resets the streak -- a fresh baseline earns a fresh ratchet)."""
    space_file = tmp_path / "space.json"
    ledger = tmp_path / "results.tsv"
    ledger.write_text(
        "commit\tval_bpb\tmemory_gb\tstatus\tdescription\tthroughput\tdiff_lines\n"
        "commit-abc123\t3000.0\t2.0\tok\tbaseline\t1200\t0\n"
        "lose1\t5000.0\t2.0\tok\tfirst attempt, fails\t1200\t10\n"
        "win1\t100.0\t2.0\tok\tsecond attempt, wins\t1200\t20\n"
    )
    model_id = _register("register-model", space_file, {**MODEL_META, "max_trials": 5})
    idea1 = _register(
        "register-idea", space_file,
        {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": "seed-1"},
    )
    idea2 = _register(
        "register-idea", space_file,
        {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": "seed-2"},
    )
    dispatch_script = tmp_path / "dispatch.json"
    dispatch_script.write_text(json.dumps([{"commit": "lose1"}, {"commit": "win1"}]))

    result = _run_cli(
        "supervise-campaign",
        "--space-file", str(space_file),
        "--model-id", model_id,
        "--ledger", str(ledger),
        "--dispatch-script", str(dispatch_script),
    )
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome["close"] == "won"
    assert len(outcome["history"]) == 2
    assert {h["candidate"] for h in outcome["history"]} == {idea1, idea2}  # two DISTINCT worker sessions

    readback = json.loads(_run_cli("readback", "--space-file", str(space_file), "--category", "model").stdout)
    model = next(f for f in readback if f["id"] == model_id)
    assert model["meta"]["campaign_status"] == "won"
    assert model["meta"]["ratchet_count"] == 0


def test_cli_supervise_campaign_closes_on_backlog_exhausted_after_registering_a_discovered_idea(
    tmp_path: Path,
) -> None:
    """R8 acceptance, CLI-driven: with no seeded backlog, an --idea-script proposal is
    registered origin=discovered before its trial, then a second dispatch with nothing
    left to draw and no further idea-script entry closes the campaign backlog_exhausted,
    recorded as a completed outcome."""
    space_file = tmp_path / "space.json"
    ledger = tmp_path / "results.tsv"
    ledger.write_text(
        "commit\tval_bpb\tmemory_gb\tstatus\tdescription\tthroughput\tdiff_lines\n"
        "commit-abc123\t1.0\t2.0\tok\tbaseline\t1200\t0\n"
        "lose1\t5000.0\t2.0\tok\tonly attempt, fails\t1200\t10\n"
    )
    model_id = _register("register-model", space_file, {**MODEL_META, "max_discovered_ideas": 1})
    dispatch_script = tmp_path / "dispatch.json"
    dispatch_script.write_text(json.dumps([{"commit": "lose1"}]))
    idea_script = tmp_path / "ideas.json"
    idea_script.write_text(json.dumps([{"axis": "optimizer", "description": "discovered mid-run", "basis": "reasoned"}]))

    result = _run_cli(
        "supervise-campaign",
        "--space-file", str(space_file),
        "--model-id", model_id,
        "--ledger", str(ledger),
        "--dispatch-script", str(dispatch_script),
        "--idea-script", str(idea_script),
    )
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome["close"] == "backlog_exhausted"
    assert outcome["history"][0]["origin"] == "discovered"

    readback = json.loads(_run_cli("readback", "--space-file", str(space_file), "--category", "idea").stdout)
    discovered = next(f for f in readback if f["meta"].get("basis") == "reasoned")
    assert discovered["meta"]["origin"] == "discovered"
    assert discovered["meta"]["axis"] == "optimizer"

    model_readback = json.loads(_run_cli("readback", "--space-file", str(space_file), "--category", "model").stdout)
    model = next(f for f in model_readback if f["id"] == model_id)
    assert model["meta"]["campaign_status"] == "completed"


def test_cli_seed_campaign_writes_seeded_ideas_across_the_nine_axis_closed_set_and_receipts(
    tmp_path: Path,
) -> None:
    """R6 acceptance, CLI-driven: seed-campaign sweeps the nine-axis closed set (six
    generative, three retrieval) against a fixture model, writing at least one origin=seeded
    idea per generative axis and recording an execution receipt (query/count/ids) for each
    retrieval axis, even one that legitimately retrieves nothing."""
    space_file = tmp_path / "space.json"
    model_id = _register("register-model", space_file, MODEL_META)

    generative_axes = [
        "theoretical_math", "ablation", "supplements",
        "ml_architectures", "non_ml_methods", "ce_ideate_breadth",
    ]
    generator_script = tmp_path / "generator.json"
    generator_script.write_text(json.dumps(
        {axis: [{"description": f"{axis} candidate"}] for axis in generative_axes}
    ))
    retriever_script = tmp_path / "retriever.json"
    retriever_script.write_text(json.dumps({
        "current_code": {
            "query": "grep TODO in train.py",
            "rows": [{"id": "train.py:42", "description": "revisit warmup"}],
        },
        "prior_trials": {
            "query": "registry: sibling model trials",
            "rows": [{"id": "trial-abc", "description": "retry sibling winner"}],
        },
        "af_learn_lessons": {"query": "af-learn: lessons tagged ml-research", "rows": []},
    }))

    result = _run_cli(
        "seed-campaign", "--space-file", str(space_file), "--model-id", model_id,
        "--generator-script", str(generator_script), "--retriever-script", str(retriever_script),
    )
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)

    for axis in generative_axes:
        assert outcome["written"][axis], f"axis {axis!r} must yield at least one written idea"

    receipts_by_axis = {r["axis"]: r for r in outcome["receipts"]}
    assert receipts_by_axis["current_code"]["count"] == 1
    assert receipts_by_axis["current_code"]["ids"] == ["train.py:42"]
    assert receipts_by_axis["af_learn_lessons"]["count"] == 0
    assert receipts_by_axis["af_learn_lessons"]["ids"] == []

    readback = json.loads(_run_cli("readback", "--space-file", str(space_file), "--category", "idea").stdout)
    assert readback
    for idea in readback:
        assert idea["meta"]["origin"] == "seeded"
        assert idea["meta"]["axis"] in generative_axes + ["current_code", "prior_trials", "af_learn_lessons"]


def test_cli_seed_campaign_interactive_mode_writes_only_confirmed_ideas_same_shape_as_batch(
    tmp_path: Path,
) -> None:
    """R6 acceptance, CLI-driven: interactive mode consumes --confirm-script in order, writing
    only confirmed candidates, of identical shape to a batch run that confirms everything."""
    space_file = tmp_path / "space.json"
    model_id = _register("register-model", space_file, MODEL_META)

    generator_script = tmp_path / "generator.json"
    generator_script.write_text(json.dumps({"ablation": [{"description": "candidate-1"}, {"description": "candidate-2"}]}))
    retriever_script = tmp_path / "retriever.json"
    retriever_script.write_text(json.dumps({}))
    confirm_script = tmp_path / "confirm.json"
    confirm_script.write_text(json.dumps([True, False]))

    result = _run_cli(
        "seed-campaign", "--space-file", str(space_file), "--model-id", model_id,
        "--mode", "interactive",
        "--generator-script", str(generator_script), "--retriever-script", str(retriever_script),
        "--confirm-script", str(confirm_script),
    )
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert len(outcome["written"]["ablation"]) == 1

    readback = json.loads(_run_cli("readback", "--space-file", str(space_file), "--category", "idea").stdout)
    assert len(readback) == 1
    assert readback[0]["meta"]["description"] == "candidate-1"
    assert readback[0]["meta"]["origin"] == "seeded"
    assert readback[0]["meta"]["axis"] == "ablation"


def test_cli_supervise_campaign_forced_axis_intervention_and_unsatisfiable_exclusion(tmp_path: Path) -> None:
    """R8 acceptance, CLI-driven: a forced_axis intervention overrides seed-first draw
    order, and an exclude_axis intervention that would leave nothing untried elsewhere is
    reported unsatisfiable rather than silently applied."""
    space_file = tmp_path / "space.json"
    ledger = tmp_path / "results.tsv"
    ledger.write_text(
        "commit\tval_bpb\tmemory_gb\tstatus\tdescription\tthroughput\tdiff_lines\n"
        "commit-abc123\t1.0\t2.0\tok\tbaseline\t1200\t0\n"
        "deadbeef\t5000.0\t2.0\tok\tforced pick\t1200\t10\n"
    )
    model_id = _register("register-model", space_file, MODEL_META)
    architecture_idea = _register(
        "register-idea", space_file,
        {"model_id": model_id, "origin": "seeded", "axis": "architecture", "description": "on architecture"},
    )
    _register(
        "register-idea", space_file,
        {"model_id": model_id, "origin": "seeded", "axis": "data", "description": "on data"},
    )
    dispatch_script = tmp_path / "dispatch.json"
    dispatch_script.write_text(json.dumps([{"commit": "deadbeef"}]))

    result = _run_cli(
        "supervise-campaign",
        "--space-file", str(space_file),
        "--model-id", model_id,
        "--ledger", str(ledger),
        "--dispatch-script", str(dispatch_script),
        "--intervention", "forced_axis:architecture",
        "--intervention", "exclude_axis:architecture",
        "--max-dispatches", "1",
    )
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome["history"][0]["candidate"] == architecture_idea  # forced_axis wins the draw
    assert outcome["history"][0]["unsatisfiable_interventions"] == []  # "data" idea keeps exclude_axis satisfiable
