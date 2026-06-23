"""Tests for the Tier-B AspectTagger + AspectJudge (gated experiment).

The tagger assigns controlled-vocabulary aspect labels to a note at write time
(a second, non-similarity recall key for the conflict path). Mirrors the judge
pattern: structured output over the LLM seam, replayed offline from a verdict
cassette, graceful skip when no source. The FakeLlm returns the JSON object
``{"tags": [...]}`` the real model is constrained to emit.
"""

import pytest

from knowledge.knowledge_graph.write_policy.write_policy_def import WriteDecision
from knowledge.knowledge_graph.write_policy.write_step_variants import AspectTagger
from knowledge.knowledge_graph.write_policy.write_step_variants.aspect_tagger import (
    _SCHEMA,
    ASPECT_VOCAB,
    AspectJudge,
)
from knowledge.llm.llm_variants.fake_llm import FakeLlm
from knowledge.llm.verdict_cassette import VerdictCassette

_PERF = '{"tags": ["performance-vs-readability"]}'
_NONE = '{"tags": []}'


def test_vocab_is_nonempty_controlled_list():
    assert ASPECT_VOCAB and all(isinstance(t, str) for t in ASPECT_VOCAB)
    assert "performance-vs-readability" in ASPECT_VOCAB


def test_judge_assigns_tags_from_vocab():
    judge = AspectJudge(llm=FakeLlm(default=_PERF))
    assert judge.tags("favor raw execution speed above all") == ["performance-vs-readability"]


def test_judge_empty_tags_on_no_aspect():
    judge = AspectJudge(llm=FakeLlm(default=_NONE))
    assert judge.tags("the office coffee machine is broken") == []


def test_judge_skips_when_no_llm_and_no_cassette():
    assert AspectJudge().tags("anything") is None  # nothing to decide with -> skip


def test_structured_output_is_constrained_to_vocab():
    # The structured schema constrains the model's tags to the controlled vocabulary.
    enum = _SCHEMA["json_schema"]["schema"]["properties"]["tags"]["items"]["enum"]
    assert enum == ASPECT_VOCAB


def test_tagger_sets_decision_tags():
    d = WriteDecision(text="favor raw execution speed above all")
    AspectTagger(judge=AspectJudge(llm=FakeLlm(default=_PERF))).apply(d)
    assert d.tags == ["performance-vs-readability"]


def test_tagger_inert_without_judge():
    d = WriteDecision(text="favor raw execution speed above all")
    AspectTagger(judge=None).apply(d)
    assert d.tags == []


def test_tagger_best_effort_when_judge_raises():
    class _BoomLlm:
        def complete(self, messages, **_):
            raise RuntimeError("no API key")

    d = WriteDecision(text="x")
    AspectTagger(judge=AspectJudge(llm=_BoomLlm())).apply(d)  # must not raise
    assert d.tags == []


def test_judge_replays_from_cassette_without_llm(tmp_path):
    path = tmp_path / "aspect.json"
    rec = AspectJudge(
        llm=FakeLlm(default=_PERF),
        cassette=VerdictCassette(path, model_id="m", allow_compute=True),
    )
    assert rec.tags("incoming note") == ["performance-vs-readability"]
    replay = AspectJudge(cassette=VerdictCassette(path, model_id="m", allow_compute=False))
    assert replay.tags("incoming note") == ["performance-vs-readability"]


def test_judge_loud_miss_when_replay_only_and_uncached(tmp_path):
    replay = AspectJudge(
        cassette=VerdictCassette(tmp_path / "aspect.json", model_id="m", allow_compute=False)
    )
    with pytest.raises(RuntimeError):
        replay.tags("incoming note")
