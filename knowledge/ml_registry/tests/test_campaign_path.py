"""Golden-path contract: the documented operator sequence as real CLI subprocesses.

Existing tests construct fixtures where metric ≈ 1.0 and throughput ≈ 1200 (or both equal
1.01), so the two meanings of ``baseline_throughput`` never disagree. This file is the
missing test kind: F1-scale metrics (~0.68) and seq/s-scale throughput (~3.5), run as
``python -m knowledge.ml_registry.cli`` with cwd = praxis repo root.

The numbers are load-bearing:

* four baseline F1s 0.6700/0.6795/0.6809/0.6811 — nearest the mean 0.677875 is 0.6795,
  not the max 0.6811
* four baseline throughputs 3.38/3.47/3.49/3.49 — VOID sits 5% below the SLOWEST (3.38),
  not below the metric mean ~0.678
* R03 at 0.7034 / 3.24 is +0.0239 over 0.6795 (well above 2σ) and only 4.1% slower than
  3.38, so it must ADOPT, not VOID
* sha:slow at 3.10 is 8% below 3.38 — voids under the default gate, adopts when
  ``void_throughput_fraction`` is 0
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
from pathlib import Path

import pytest

# knowledge/ml_registry/tests -> praxis repo root (parents[3], not 2).
REPO_ROOT = Path(__file__).resolve().parents[3]

LEDGER_V2_HEADER = (
    "commit\tmetric_value\tmemory_gb\tstatus\tdescription\tthroughput\tdiff_lines\n"
)

# commit, metric_value, throughput, description. Descriptions starting "baseline"
# are the noise-floor / VOID-gate sample.
LEDGER_ROWS: list[tuple[str, float, float, str]] = [
    ("sha:baseline_1", 0.6809, 3.49, "baseline_1 | head=gru"),
    ("sha:baseline_2", 0.6700, 3.47, "baseline_2 | head=gru"),
    ("sha:baseline_3", 0.6811, 3.49, "baseline_3 | head=gru"),
    ("sha:baseline_4", 0.6795, 3.38, "baseline_4 | head=gru"),  # nearest mean; slowest
    ("sha:R03", 0.7034, 3.24, "R03 | bones"),                  # 4.1% slower than 3.38
    ("sha:R01", 0.6810, 3.40, "R01 | drop_face"),              # inside the floor
    ("sha:slow", 0.7034, 3.10, "slow | structurally slower"),  # 8% below 3.38
]

BASELINE_VALUES = (0.6809, 0.6700, 0.6811, 0.6795)
BASELINE_COMMITS = (
    "sha:baseline_1",
    "sha:baseline_2",
    "sha:baseline_3",
    "sha:baseline_4",
)

BACKLOG: list[dict] = [
    {"id": "R04", "axis": "representation", "hypothesis": "velocity", "basis": "skip me"},
    {"id": "R01", "axis": "representation", "stage": "representation", "hypothesis": "drop face"},
    {"id": "R03", "axis": "representation", "stage": "representation", "hypothesis": "bones"},
    {"id": "R07", "axis": "representation", "stage": "representation",
     "depends_on": ["R01"], "hypothesis": "compose"},
    {"id": "M01", "axis": "architecture", "stage": "architecture", "hypothesis": "gru"},
]


def _cli(*args: object) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "knowledge.ml_registry.cli", *[str(a) for a in args]],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _write_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    ledger = tmp_path / "results.tsv"
    ledger.write_text(
        LEDGER_V2_HEADER
        + "".join(
            f"{commit}\t{value}\t0.0\tok\t{desc}\t{tput}\t0\n"
            for commit, value, tput, desc in LEDGER_ROWS
        )
    )
    backlog = tmp_path / "backlog.jsonl"
    backlog.write_text("".join(json.dumps(row) + "\n" for row in BACKLOG))
    return ledger, backlog, tmp_path / "registry", tmp_path / "space.json"


def _bootstrap(tmp_path: Path, *extra: object) -> tuple[subprocess.CompletedProcess, Path, Path, Path]:
    ledger, backlog, out_dir, space = _write_inputs(tmp_path)
    result = _cli(
        "bootstrap-campaign",
        "--ledger", ledger,
        "--backlog", backlog,
        "--model-id", "stroke_clf",
        "--metric", "stroke_macro_f1",
        "--direction", "maximize",
        "--diff-size-limit", "8",
        "--skip-ids", "R04",
        "--out-dir", out_dir,
        *extra,
    )
    return result, ledger, out_dir, space


def _two_sigma() -> float:
    return 2.0 * statistics.stdev(BASELINE_VALUES)


def _register_model(space: Path, meta_json: Path, ledger: Path) -> subprocess.CompletedProcess:
    return _cli(
        "register-model-with-baseline",
        "--space-file", space,
        "--meta-json", meta_json,
        "--ledger", ledger,
    )


def _register_ideas(space: Path, ideas_path: Path, model_id: str) -> dict[str, str]:
    """Rewrite each idea's model_id to the minted model fact id, then register."""
    minted: dict[str, str] = {}
    for line in ideas_path.read_text().splitlines():
        if not line.strip():
            continue
        idea = json.loads(line)
        idea["model_id"] = model_id
        result = _cli("register-idea", "--space-file", space, "--meta-json", json.dumps(idea))
        assert result.returncode == 0, result.stderr + result.stdout
        minted[str(idea["id"])] = result.stdout.strip().rsplit(" ", 1)[-1]
    return minted


def _register_trial(space: Path, ledger: Path, model_id: str, idea_id: str, commit: str) -> str:
    # Do NOT pass throughput/diff_lines -- U4 copies them from the ledger.
    result = _cli(
        "register-trial",
        "--space-file", space,
        "--meta-json", json.dumps({
            "model_id": model_id,
            "idea_id": idea_id,
            "commit": commit,
            "status": "complete",
        }),
        "--ledger", ledger,
        "--json",
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout)["trial_id"]


def test_documented_bootstrap_then_register_then_verdict_path(tmp_path: Path) -> None:
    boot, ledger, out_dir, space = _bootstrap(tmp_path)
    assert boot.returncode == 0, boot.stderr + boot.stdout
    report = json.loads(boot.stdout)
    assert report["ready"] is True

    meta = json.loads((out_dir / "model_meta.json").read_text())
    # Nearest the mean, not the max 0.6811. Throughput is min seq/s, not mean F1 ~0.678.
    two_sigma = _two_sigma()
    assert meta["baseline"] == "sha:baseline_4"
    assert meta["baseline_throughput"] == pytest.approx(3.38)
    assert meta.get("sigmas") == 2.0
    # Bootstrap rounds to 6 d.p.; still must be 2σ, not the 1σ R12 default.
    assert meta["noise_floor"] == pytest.approx(two_sigma, abs=1e-5)
    assert meta["noise_floor"] != pytest.approx(two_sigma / 2.0, abs=1e-4)
    assert "baseline_runs" in meta
    assert len(meta["baseline_runs"]) == 4
    assert set(meta["baseline_runs"]) == set(BASELINE_COMMITS)

    registered = _register_model(space, out_dir / "model_meta.json", ledger)
    assert registered.returncode == 0, registered.stderr + registered.stdout
    model_id = registered.stdout.strip().rsplit(" ", 1)[-1]
    assert model_id.startswith("model-"), model_id

    readback = _cli("readback", "--space-file", space, "--category", "model")
    assert readback.returncode == 0, readback.stderr + readback.stdout
    stored = json.loads(readback.stdout)[0]["meta"]
    assert stored["baseline"] == "sha:baseline_4"
    assert stored["baseline_throughput"] == pytest.approx(3.38)
    assert stored["noise_floor"] == pytest.approx(two_sigma, abs=1e-5)
    assert stored["noise_floor"] != pytest.approx(two_sigma / 2.0, abs=1e-4)

    idea_ids = _register_ideas(space, out_dir / "ideas.jsonl", model_id)
    assert "R03" in idea_ids

    trial_id = _register_trial(space, ledger, model_id, idea_ids["R03"], "sha:R03")
    assert trial_id.startswith("trial-"), trial_id

    verdict = _cli(
        "resolve-verdict",
        "--space-file", space,
        "--trial-id", trial_id,
        "--ledger", ledger,
        "--json",
    )
    assert verdict.returncode == 0, verdict.stderr + verdict.stdout
    # 0.7034 vs 0.6795 is +0.0239, well above 2σ of the four baseline F1s; 3.24 is
    # only 4.1% slower than min 3.38, so the default 5% VOID gate must not fire.
    assert json.loads(verdict.stdout)["verdict"] == "adopted"

    wrong_kind = _cli(
        "resolve-verdict",
        "--space-file", space,
        "--trial-id", idea_ids["R03"],
        "--ledger", ledger,
        "--json",
    )
    assert wrong_kind.returncode != 0
    assert "trial" in (wrong_kind.stdout + wrong_kind.stderr).lower()


def test_void_fraction_zero_adopts_a_structurally_slower_winner(tmp_path: Path) -> None:
    """CV campaigns whose metric is not training speed disable VOID rather than
    hacking ``baseline_throughput=0.01``. 3.10 is 8% below 3.38 and would void
    under the default 0.05 fraction."""
    boot, ledger, out_dir, space = _bootstrap(tmp_path, "--void-throughput-fraction", "0")
    assert boot.returncode == 0, boot.stderr + boot.stdout
    meta = json.loads((out_dir / "model_meta.json").read_text())
    assert float(meta["void_throughput_fraction"]) == 0.0

    registered = _register_model(space, out_dir / "model_meta.json", ledger)
    assert registered.returncode == 0, registered.stderr + registered.stdout
    model_id = registered.stdout.strip().rsplit(" ", 1)[-1]

    idea_ids = _register_ideas(space, out_dir / "ideas.jsonl", model_id)
    trial_id = _register_trial(space, ledger, model_id, idea_ids["R03"], "sha:slow")

    verdict = _cli(
        "resolve-verdict",
        "--space-file", space,
        "--trial-id", trial_id,
        "--ledger", ledger,
        "--json",
    )
    assert verdict.returncode == 0, verdict.stderr + verdict.stdout
    assert json.loads(verdict.stdout)["verdict"] == "adopted"


def test_next_queue_opens_architecture_after_park_and_adopt() -> None:
    """After R01 parked and R03 adopted, R07 is unreachable; architecture must open.

    Import inside the test so a sibling that has not landed ``next_queue`` /
    ``StagingStuck`` does not hide the CLI assertions above at collection time.
    """
    try:
        from knowledge.ml_registry.staging import StagingStuck, next_queue
    except ImportError:
        pytest.skip("next_queue / StagingStuck not landed yet")

    items = [
        {"id": "R04", "axis": "representation", "stage": "representation"},
        {"id": "R01", "axis": "representation", "stage": "representation"},
        {"id": "R03", "axis": "representation", "stage": "representation"},
        {"id": "R07", "axis": "representation", "stage": "representation", "depends_on": ["R01"]},
        {"id": "M01", "axis": "architecture", "stage": "architecture"},
    ]
    stages = ("representation", "architecture")
    stage, queue, blocked = next_queue(
        items,
        answered_ids={"R01", "R03", "R04"},
        adopted_ids={"R03"},
        stages=stages,
    )
    assert stage == "architecture"
    assert [i["id"] for i in queue] == ["M01"]
    assert "R07" in blocked
    # StagingStuck is the loud failure when skip/out-of-scope never joined answered.
    assert issubclass(StagingStuck, Exception)
