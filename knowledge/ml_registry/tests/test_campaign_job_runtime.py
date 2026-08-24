from __future__ import annotations

import json
from pathlib import Path
import sys

from knowledge.ml_registry.contracts import CampaignOutcome, ProductionAliasRef
from knowledge.ml_registry.runtime.agent_session import AgentSession
from knowledge.ml_registry.runtime.campaign_job import (
    DEFAULT_CAMPAIGN_DISK_BUDGET_BYTES,
    CampaignJob,
    CampaignJobContext,
)
from knowledge.ml_registry.runtime.progress import parse_progress_line, read_progress_snapshot


class _Adapter:
    def __init__(self, marker: Path, *, stalled: bool = False) -> None:
        self.marker = marker
        self.stalled = stalled
        self.beats = 0
        self.void_reasons: list[str] = []
        self.last_context = None

    def preflight(self, _context: CampaignJobContext) -> None:
        return None

    def setup(self, _context: CampaignJobContext) -> None:
        return None

    def complete(self, _context: CampaignJobContext) -> ProductionAliasRef | None:
        if self.marker.exists() and not self.stalled:
            return ProductionAliasRef("model-fixture", 1)
        return None

    def blocking_diagnosis(self, _context: CampaignJobContext) -> None:
        return None

    def trial_count(self, _context: CampaignJobContext) -> int:
        return (1 if self.marker.exists() else 0) + len(self.void_reasons)

    def dispatch_one(self, _context: CampaignJobContext) -> list[str]:
        source = (
            "print('[progress] fixture arm 1/1 100% elapsed 0m01s eta 0m00s last=0.7500', flush=True);"
            + ("" if self.stalled else f"open({str(self.marker)!r}, 'w').write('run')")
        )
        return [sys.executable, "-c", source]

    def heartbeat(self, context: CampaignJobContext) -> None:
        self.beats += 1
        self.last_context = context

    def void_arm(self, _context: CampaignJobContext, reason: str) -> None:
        self.void_reasons.append(reason)


def _job(tmp_path: Path, adapter: _Adapter) -> CampaignJob:
    context = CampaignJobContext("fixture", 1, tmp_path, tmp_path / "progress.json")
    return CampaignJob(context=context, adapter=adapter, outcome_path=tmp_path / "outcome.json",
                       heartbeat_s=.01, working_directory=tmp_path)


def test_campaign_job_runs_one_arm_at_a_time_and_writes_typed_promotion(tmp_path: Path) -> None:
    adapter = _Adapter(tmp_path / "run.marker")
    outcome = _job(tmp_path, adapter).run()

    assert outcome.outcome is CampaignOutcome.PROMOTED
    assert outcome.production_alias == ProductionAliasRef("model-fixture", 1)
    assert json.loads((tmp_path / "outcome.json").read_text())["outcome"] == "PROMOTED"
    progress = read_progress_snapshot(tmp_path / "progress.json")
    assert progress is not None and progress.current == progress.total == 1


def test_campaign_job_classifies_zero_new_registry_runs_as_stalled(tmp_path: Path) -> None:
    adapter = _Adapter(tmp_path / "never.marker", stalled=True)
    outcome = _job(tmp_path, adapter).run()
    assert outcome.outcome is CampaignOutcome.STALLED
    assert "no new registry run" in outcome.reason


def test_progress_parser_rejects_noncanonical_and_parses_eta() -> None:
    assert parse_progress_line("still working") is None
    parsed = parse_progress_line(
        "[progress] fold eval 3/8 38% elapsed 1m02s eta 1m44s last=0.8125\n"
    )
    assert parsed is not None
    assert (parsed.current, parsed.total, parsed.eta, parsed.latest_metric) == (3, 8, "1m44s", .8125)


def test_agent_session_classifies_quota_without_relaunching(tmp_path: Path) -> None:
    session = AgentSession(
        command=[sys.executable, "-c", "print('usage limit reached')"], cwd=tmp_path,
        progress_path=tmp_path / "progress.json", completion=lambda: None,
        max_relaunches=4,
    )
    result = session.run()
    assert result.outcome is CampaignOutcome.QUOTA
    assert result.launches == 1


def test_wall_clock_cap_kills_and_voids_an_arm_before_continuing(tmp_path: Path) -> None:
    marker = tmp_path / "trial.marker"

    class Adapter(_Adapter):
        def dispatch_one(self, _context: CampaignJobContext) -> list[str]:
            return [sys.executable, "-c", "import time; time.sleep(1)"]

        def terminal_outcome(
            self, _context: CampaignJobContext,
        ) -> tuple[CampaignOutcome, str] | None:
            if self.void_reasons:
                return CampaignOutcome.MEASURED, "stagnation advanced through the void"
            return None

    adapter = Adapter(marker)
    outcome = CampaignJob(
        context=CampaignJobContext("fixture", 1, tmp_path, tmp_path / "progress.json"),
        adapter=adapter,
        outcome_path=tmp_path / "outcome.json",
        arm_timeout_s=.05,
        heartbeat_s=1,
        working_directory=tmp_path,
    ).run()

    assert outcome.outcome is CampaignOutcome.MEASURED
    assert adapter.void_reasons and "VOIDED on throughput" in adapter.void_reasons[0]
    assert "wall-clock cap" in adapter.void_reasons[0]
    assert not marker.exists()


def test_missing_progress_heartbeat_kills_and_voids_a_live_process(tmp_path: Path) -> None:
    marker = tmp_path / "trial.marker"

    class Adapter(_Adapter):
        def dispatch_one(self, _context: CampaignJobContext) -> list[str]:
            source = (
                "import time; "
                "print('[progress] arm 1/2 50% elapsed 0m01s eta 0m01s', flush=True); "
                "time.sleep(1)"
            )
            return [sys.executable, "-c", source]

        def terminal_outcome(
            self, _context: CampaignJobContext,
        ) -> tuple[CampaignOutcome, str] | None:
            if self.void_reasons:
                return CampaignOutcome.MEASURED, "heartbeat void advanced stagnation"
            return None

    adapter = Adapter(marker)
    outcome = CampaignJob(
        context=CampaignJobContext("fixture", 1, tmp_path, tmp_path / "progress.json"),
        adapter=adapter,
        outcome_path=tmp_path / "outcome.json",
        arm_timeout_s=1,
        heartbeat_s=.05,
        working_directory=tmp_path,
    ).run()

    assert outcome.outcome is CampaignOutcome.MEASURED
    assert adapter.beats == 1
    assert adapter.void_reasons and "progress heartbeat" in adapter.void_reasons[0]
    assert adapter.last_context.progress_heartbeat_cadence_s == .05


def test_progress_inside_declared_cadence_is_not_mistaken_for_a_hang(tmp_path: Path) -> None:
    marker = tmp_path / "trial.marker"

    class Adapter(_Adapter):
        def dispatch_one(self, _context: CampaignJobContext) -> list[str]:
            source = (
                "import time; "
                "[(print(f'[progress] arm {i}/4 {i * 25}% elapsed 0m01s eta 0m01s', flush=True), "
                "time.sleep(.02)) for i in range(1, 5)]; "
                f"open({str(marker)!r}, 'w').write('run')"
            )
            return [sys.executable, "-c", source]

    adapter = Adapter(marker)
    outcome = CampaignJob(
        context=CampaignJobContext("fixture", 1, tmp_path, tmp_path / "progress.json"),
        adapter=adapter,
        outcome_path=tmp_path / "outcome.json",
        arm_timeout_s=1,
        heartbeat_s=.05,
        working_directory=tmp_path,
    ).run()

    assert outcome.outcome is CampaignOutcome.PROMOTED
    assert adapter.beats == 4
    assert adapter.void_reasons == []


def test_disk_budget_includes_external_corpus_cache_before_dispatch(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    corpus_cache = tmp_path / "other-repo" / "data" / "cache"
    state_root.mkdir()
    corpus_cache.mkdir(parents=True)
    (state_root / "checkpoint.bin").write_bytes(b"a" * 8)
    (corpus_cache / "corpus.bin").write_bytes(b"b" * 9)
    adapter = _Adapter(tmp_path / "never.marker")

    outcome = CampaignJob(
        context=CampaignJobContext("fixture", 1, state_root, state_root / "progress.json"),
        adapter=adapter,
        outcome_path=state_root / "outcome.json",
        disk_budget_bytes=16,
        corpus_cache_root=corpus_cache,
        working_directory=tmp_path,
    ).run()

    assert outcome.outcome is CampaignOutcome.ABANDONED
    assert "disk budget" in outcome.reason
    assert "17 bytes" in outcome.reason
    assert not adapter.marker.exists()


def test_unreadable_corpus_cache_refuses_launch_instead_of_counting_zero(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    missing_cache = tmp_path / "other-repo" / "data" / "cache"
    adapter = _Adapter(tmp_path / "never.marker")

    outcome = CampaignJob(
        context=CampaignJobContext("fixture", 1, state_root, state_root / "progress.json"),
        adapter=adapter,
        outcome_path=state_root / "outcome.json",
        corpus_cache_root=missing_cache,
        working_directory=tmp_path,
    ).run()

    assert outcome.outcome is CampaignOutcome.ABANDONED
    assert "cannot read disk usage" in outcome.reason
    assert str(missing_cache) in outcome.reason
    assert not adapter.marker.exists()


def test_default_campaign_disk_budget_is_stated_and_finite() -> None:
    assert DEFAULT_CAMPAIGN_DISK_BUDGET_BYTES == 50 * 1024**3
