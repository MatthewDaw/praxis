"""Constitution IV's hard restart is an event-log fact, never a rejection or an alias move.

A champion converging below 0.60 is not in a weakness loop -- its framing is wrong -- and the
constitution says the ledger "records RESTARTED with the reason, never a rejection". There was no
seam for that, so three campaigns were being told every sweep to record something they had no way
to write. The alternatives all corrupted evidence: the only writable reason-carrying events belong
to a RUN, and abandoning a fairly judged run to carry a campaign-level fact rewrites a verdict the
judge did reach.

What these tests pin is what a restart must NOT do: it must not touch the champion, the runs, the
experiments row or the bar, because a restart re-poses the search and never lowers it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge.ml_registry.storage import Registry, RegistryError, replay_projection


def test_a_restart_is_logged_with_its_marker_and_reason(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    assert registry.record_campaign_restarted(
        campaign_id="a01_baseball_object_detection",
        reason="constitution IV; brief /workspace/overnight/briefs/a01-hard-restart.md",
    ) is True
    events = [e for e in registry.list_events() if e.event_type == "campaign_restarted"]
    assert len(events) == 1
    payload = events[0].payload
    assert payload["marker"] == "RESTARTED"
    assert payload["campaign_id"] == payload["experiment_id"] == "a01_baseball_object_detection"
    # campaign_health.py finds the record by grepping the encoded payload for both of these.
    encoded = json.dumps(payload)
    assert "RESTARTED" in encoded and "a01_baseball_object_detection" in encoded
    assert "a01-hard-restart.md" in payload["reason"]


def test_a_restart_changes_no_projected_state(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    registry.record_campaign_restarted(campaign_id="a05_event_spotting", reason="frame re-posed")
    assert set(registry.table_names()) == {
        "experiments", "runs", "artifacts", "registered_models", "model_versions",
        "lineage", "aliases", "events",
    }
    assert registry.rows("aliases") == []
    assert registry.rows("runs") == []
    assert registry.rows("experiments") == []


def test_a_restart_needs_no_registered_experiment(tmp_path: Path) -> None:
    # IV most often fires on a campaign that is still seeding; requiring a registered
    # experiment would make the record unwritable exactly when it is needed.
    registry = Registry(tmp_path)
    assert registry.record_campaign_restarted(campaign_id="not_registered", reason="re-posed") is True


def test_the_same_restart_is_idempotent_and_a_new_reason_is_a_new_restart(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    kwargs = {"campaign_id": "a04_ball_possession", "reason": "the ball was withheld"}
    assert registry.record_campaign_restarted(**kwargs) is True
    count = len(registry.list_events())
    assert registry.record_campaign_restarted(**kwargs) is False
    assert len(registry.list_events()) == count
    assert registry.record_campaign_restarted(campaign_id="a04_ball_possession",
                                              reason="second re-posing") is True
    reasons = [e.payload["reason"] for e in registry.list_events()
               if e.event_type == "campaign_restarted"]
    assert reasons == ["the ball was withheld", "second re-posing"]


def test_a_restart_replays_from_the_event_log(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    registry.record_campaign_restarted(campaign_id="a01", reason="re-posed")
    replay_projection(tmp_path)
    assert [e.event_type for e in registry.list_events()][-1] == "campaign_restarted"


def test_a_restart_requires_an_id_and_a_reason(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    with pytest.raises(RegistryError, match="id and reason"):
        registry.record_campaign_restarted(campaign_id="  ", reason="because")
    with pytest.raises(RegistryError, match="id and reason"):
        registry.record_campaign_restarted(campaign_id="a01", reason="  ")
    assert [e for e in registry.list_events() if e.event_type == "campaign_restarted"] == []
