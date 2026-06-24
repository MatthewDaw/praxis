"""U4: CaptionCassette replay / record / loud-miss."""

from __future__ import annotations

import pytest

from knowledge.llm.caption_cassette import CaptionCassette


def _cassette(tmp_path, *, allow_compute, model="m/vlm", pv="prompt-a"):
    return CaptionCassette(
        tmp_path / "caps.json", model_id=model, prompt=pv, allow_compute=allow_compute
    )


def test_hit_returns_cached_without_compute(tmp_path):
    cas = _cassette(tmp_path, allow_compute=True)
    calls: list[int] = []
    cas.caption("hashA", lambda: (calls.append(1), "first")[1])
    # second call for the same payload hits cache; compute must not fire again
    out = cas.caption("hashA", lambda: (calls.append(1), "second")[1])
    assert out == "first"
    assert len(calls) == 1


def test_miss_computes_and_records(tmp_path):
    cas = _cassette(tmp_path, allow_compute=True)
    out = cas.caption("hashB", lambda: "a blue pixel mascot")
    assert out == "a blue pixel mascot"
    # a fresh cassette over the same file replays it (persisted)
    cas2 = _cassette(tmp_path, allow_compute=False)
    assert cas2.caption("hashB", lambda: "should not compute") == "a blue pixel mascot"


def test_model_or_prompt_change_is_a_miss(tmp_path):
    _cassette(tmp_path, allow_compute=True, model="m/a", pv="prompt-a").caption("h", lambda: "cap-a")
    # same payload, different model id -> miss
    other_model = _cassette(tmp_path, allow_compute=False, model="m/b", pv="prompt-a")
    with pytest.raises(RuntimeError):
        other_model.caption("h", lambda: "x")
    # same payload+model, different prompt text -> miss
    other_prompt = _cassette(tmp_path, allow_compute=False, model="m/a", pv="prompt-b")
    with pytest.raises(RuntimeError):
        other_prompt.caption("h", lambda: "x")


def test_miss_without_compute_raises_loud(tmp_path):
    cas = _cassette(tmp_path, allow_compute=False)
    with pytest.raises(RuntimeError):
        cas.caption("never-seen", lambda: "x")
