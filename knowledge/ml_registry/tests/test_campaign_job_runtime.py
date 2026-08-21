from __future__ import annotations

import json
from pathlib import Path
import sys

from knowledge.ml_registry.contracts import CampaignOutcome, ProductionAliasRef
from knowledge.ml_registry.runtime.agent_session import AgentSession
from knowledge.ml_registry.runtime.campaign_job import CampaignJob, CampaignJobContext
from knowledge.ml_registry.runtime.progress import parse_progress_line, read_progress_snapshot


class _Adapter:
    def __init__(self, marker: Path, *, stalled: bool = False) -> None:
        self.marker = marker
        self.stalled = stalled
        self.beats = 0

    def preflight(self, _context):
        return None

    def setup(self, _context):
        return None

    def complete(self, _context):
        if self.marker.exists() and not self.stalled:
            return ProductionAliasRef("model-fixture", 1)
        return None

    def blocking_diagnosis(self, _context):
        return None

    def trial_count(self, _context):
        return 1 if self.marker.exists() else 0

    def dispatch_one(self, _context):
        source = (
            "print('[progress] fixture arm 1/1 100% elapsed 0m01s eta 0m00s last=0.7500', flush=True);"
            + ("" if self.stalled else f"open({str(self.marker)!r}, 'w').write('run')")
        )
        return [sys.executable, "-c", source]

    def heartbeat(self, _context):
        self.beats += 1


def _job(tmp_path: Path, adapter: _Adapter) -> CampaignJob:
    context = CampaignJobContext("fixture", 1, tmp_path, tmp_path / "progress.json")
    return CampaignJob(context=context, adapter=adapter, outcome_path=tmp_path / "outcome.json",
                       heartbeat_s=.01, working_directory=tmp_path)


def test_campaign_job_runs_one_arm_at_a_time_and_writes_typed_complete(tmp_path: Path) -> None:
    adapter = _Adapter(tmp_path / "run.marker")
    outcome = _job(tmp_path, adapter).run()

    assert outcome.outcome is CampaignOutcome.COMPLETE
    assert outcome.production_alias == ProductionAliasRef("model-fixture", 1)
    assert json.loads((tmp_path / "outcome.json").read_text())["outcome"] == "COMPLETE"
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
