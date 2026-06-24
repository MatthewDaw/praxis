"""End-to-end tests for the eval harness runner/grader/registry.

Grounding-aware rubric judge (feature 004) — affected cases & checks under test:
  * Reference threading: the 14 ``matt/applications/*`` cases, ``matt_volta_video_mock``,
    and ``safety_user_overrides_graph`` carry a seeded reference that ``grade_rubric``
    now passes to the judge (grounding/honesty criteria verify support, not plausibility).
  * Widened deterministic checks (US3): the brittle literal-keyword
    ``regex_matches`` checks on the matt cases (e.g. ``(?i)rag``, ``(?i)sql``,
    ``(?i)billions``, ``(?i)utah``) are widened to synonym-tolerant ``mentions_any``
    sets — none removed (FR-010b).
  * No-regression: the reference-free rubric cases must get a byte-identical prompt
    (no REFERENCE block) — SC-004 / SC-006.
"""

import json

from knowledge.evals.eval_def import EvalCase, EvalContext
from knowledge.evals.run import (
    FakeRunner,
    build_transcript,
    case_needs,
    load_case,
    load_cases,
    partition_by_capability,
    resolve_check,
    run_case,
    run_case_full,
    status_of,
    unmet_needs,
    write_baseline,
    write_transcript,
)
from knowledge.evals.eval_def import CaseResult


class _SandboxRunner:
    """Stub runner that advertises the sandbox capability."""

    provides = frozenset({"sandbox"})

    def run(self, case, reader):  # pragma: no cover - not exercised
        return EvalContext(case_id=case.id, output="")


def _case(**overrides):
    base = dict(
        id="c1",
        seed_prompt="add(a, b)",
        target_commit="abc123",
        deterministic_checks=[
            {
                "name": "defines_add",
                "ref": "knowledge.evals.deterministic_checks.builds:contains_text",
                "params": {"text": "def add"},
            }
        ],
    )
    base.update(overrides)
    return EvalCase.model_validate(base)


def test_weighted_overall_uses_declared_weights():
    from knowledge.evals.eval_def import Rubric, RubricItem, weighted_overall

    rubric = Rubric(
        id="r",
        items=[
            RubricItem(id="scope", criterion="...", weight=3.0),
            RubricItem(id="fn", criterion="...", weight=1.0),
        ],
    )
    # heavily-weighted criterion fails, light one passes -> 0.25, not the 0.5 mean
    assert weighted_overall(rubric, {"scope": 0.0, "fn": 1.0}) == 0.25
    assert weighted_overall(rubric, {"fn": 1.0}) == 0.25  # missing item counts as 0
    assert weighted_overall(rubric, {"scope": 1.0, "fn": 1.0}) == 1.0
    assert weighted_overall(Rubric(id="r", items=[]), {}) == 0.0  # no items -> 0


def test_align_per_item_handles_ids_and_positional_fallback():
    from knowledge.evals.eval_def import Rubric, RubricItem, align_per_item

    rubric = Rubric(
        id="r", items=[RubricItem(id="a", criterion="..."), RubricItem(id="b", criterion="...")]
    )
    assert align_per_item(rubric, {"a": 1.0}) == {"a": 1.0, "b": 0.0}  # exact ids, missing -> 0
    assert align_per_item(rubric, {"1": 0.0, "2": 1.0}) == {"a": 0.0, "b": 1.0}  # positional
    assert align_per_item(rubric, {"x": 1.0}) == {"a": 0.0, "b": 0.0}  # no match, bad count -> 0
    assert align_per_item(rubric, None) == {"a": 0.0, "b": 0.0}


def test_resolve_check_imports_callable():
    from knowledge.evals.eval_def import DeterministicCheckRef

    func = resolve_check(
        DeterministicCheckRef(
            name="x", ref="knowledge.evals.deterministic_checks.builds:output_nonempty"
        )
    )
    assert callable(func)


def test_passing_run_is_passed():
    runner = FakeRunner(scripted={"c1": "def add(a, b):\n    return a + b\n"})
    result = run_case(_case(), runner)
    assert result.passed is True
    assert all(c.passed for c in result.checks)


def test_empty_output_fails_baseline():
    # The "expected to fail" baseline: FakeRunner produces nothing.
    result = run_case(_case(), FakeRunner())
    assert result.passed is False
    assert any(not c.passed for c in result.checks)


def test_seeded_knowledge_is_available_to_reader():
    # A runner that surfaces what the reader returns proves seeding wired through.
    class ReaderEchoRunner:
        def run(self, case, reader):
            return EvalContext(case_id=case.id, output=reader.read())

    case = _case(
        deterministic_checks=[
            {
                "name": "has_seed",
                "ref": "knowledge.evals.deterministic_checks.builds:contains_text",
                "params": {"text": "seeded fact"},
            }
        ],
        seeded_insight={"direct_to_graph": ["seeded fact"]},
    )
    result = run_case(case, ReaderEchoRunner())
    assert result.passed is True


def test_write_baseline_appends_one_row_per_case(tmp_path):
    path = tmp_path / "baseline.jsonl"
    results = [run_case(_case(), FakeRunner())]
    write_baseline(results, path)
    write_baseline(results, path)  # append, not overwrite
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["case_id"] == "c1"


def _write_case_yaml(case_dir):
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "case.yaml").write_text(
        "id: c1\n"
        "seed_prompt: edit calculator.py\n"
        "target_commit: abc123\n"
        "deterministic_checks:\n"
        "  - name: defines_add\n"
        "    ref: knowledge.evals.deterministic_checks.builds:contains_text\n"
        "    params: {text: 'def add'}\n",
        encoding="utf-8",
    )


def test_load_case_records_sibling_fixture_dir(tmp_path):
    case_dir = tmp_path / "case"
    _write_case_yaml(case_dir)
    (case_dir / "fixture").mkdir()
    (case_dir / "fixture" / "calculator.py").write_text("x = 1\n", encoding="utf-8")

    case = load_case(case_dir)
    assert case.fixture_path == str((case_dir / "fixture").resolve())


def test_load_case_without_fixture_leaves_path_none(tmp_path):
    case_dir = tmp_path / "case"
    _write_case_yaml(case_dir)
    assert load_case(case_dir).fixture_path is None


class _CaptureRunner:
    """Runner that reports provenance, to prove the transcript captures it."""

    def run(self, case, reader):
        return EvalContext(
            case_id=case.id,
            output="def add(a, b):\n    return a + b\n",
            raw_response='{"result": "done", "total_cost_usd": 0.01}',
            output_source="named_file",
            injected_knowledge="prefer terse code",
        )


def test_transcript_captures_raw_response_and_verdict():
    case = _case()
    ctx, judge_result, verdict = run_case_full(case, _CaptureRunner())
    transcript = build_transcript(case, ctx, judge_result, verdict, run_id="run1")

    assert transcript.run_id == "run1"
    assert transcript.case_id == "c1"
    assert transcript.injected_knowledge == "prefer terse code"
    assert transcript.agent.raw_response == '{"result": "done", "total_cost_usd": 0.01}'
    assert transcript.agent.output_source == "named_file"
    assert transcript.verdict.passed is True
    assert transcript.judge is None  # no rubric on this case


def test_write_transcript_lands_file_under_run_id(tmp_path):
    case = _case()
    ctx, judge_result, verdict = run_case_full(case, _CaptureRunner())
    transcript = build_transcript(case, ctx, judge_result, verdict, run_id="run1")

    path = write_transcript(transcript, runs_dir=tmp_path)
    assert path == tmp_path / "run1" / "c1.json"
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["agent"]["raw_response"] == '{"result": "done", "total_cost_usd": 0.01}'
    assert written["verdict"]["passed"] is True


def test_explicit_needs_skipped_without_capability():
    case = _case(needs=["sandbox"])
    assert case_needs(case) == {"sandbox"}
    # A runner that provides nothing can't grade it; one that provides sandbox can.
    assert unmet_needs(case, FakeRunner()) == {"sandbox"}
    assert unmet_needs(case, _SandboxRunner()) == set()


def test_fixtures_imply_sandbox_and_code_task_implies_code_exec():
    assert case_needs(_case(fixture_path="/tmp/box")) == {"sandbox"}
    code_case = _case(
        code_task={
            "repo": "more-itertools/more-itertools",
            "base_commit": "abc",
            "target_commit": "def",
            "fail_to_pass": ["t"],
        }
    )
    assert "code_exec" in case_needs(code_case)


def test_file_artifact_checks_auto_derive_file_io():
    # A writes_file/modifies_file check reads ctx.artifacts, which only a file-
    # producing runner populates — derive file_io so the case skips on a text-only
    # backend (but runs on Claude OR the structured runner, both of which provide it).
    for ref in (
        "knowledge.evals.deterministic_checks.builds:writes_file",
        "knowledge.evals.deterministic_checks.builds:modifies_file",
    ):
        case = _case(deterministic_checks=[{"name": "f", "ref": ref, "params": {"path": "answer.txt"}}])
        assert case_needs(case) == {"file_io"}
        assert unmet_needs(case, FakeRunner()) == {"file_io"}


def test_file_io_split_routes_fixture_cases_away_from_structured_runner():
    from knowledge.evals.claude_code import ClaudeCodeRunner
    from knowledge.evals.openrouter import StructuredOpenRouterRunner

    # Claude provides both; the structured runner provides only file_io.
    assert {"sandbox", "file_io"} <= ClaudeCodeRunner.provides
    assert StructuredOpenRouterRunner.provides == frozenset({"file_io"})

    structured = StructuredOpenRouterRunner()
    # A file-only case runs on the structured runner; a fixture case still needs the box.
    file_only = _case(needs=["file_io"])
    fixture_case = _case(fixture_path="/tmp/box")
    assert unmet_needs(file_only, structured) == set()
    assert unmet_needs(fixture_case, structured) == {"sandbox"}


def test_embedder_axis_auto_derives_capability():
    # cached needs real_embeddings (cache or key); live needs live_embeddings (key only).
    assert case_needs(_case(embedder="cached")) == {"real_embeddings"}
    assert case_needs(_case(embedder="live")) == {"live_embeddings"}
    assert "real_embeddings" not in case_needs(_case())  # fake default needs nothing extra


def test_real_embedding_cases_skip_without_cache_or_key(monkeypatch, tmp_path):
    import knowledge.evals.run as run_mod

    monkeypatch.setattr(run_mod, "EMBED_CACHE_DIR", tmp_path)  # empty -> no committed fixture
    monkeypatch.setattr(run_mod, "VERDICT_CACHE_DIR", tmp_path)  # empty -> no merge cassette
    monkeypatch.setattr(run_mod, "INGEST_CACHE_DIR", tmp_path)  # empty -> no ingestion cassette
    monkeypatch.setattr(run_mod, "CAPTION_CACHE_DIR", tmp_path)  # empty -> no caption cassette
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_EMBED_MODEL", raising=False)

    assert run_mod.harness_capabilities() == set()
    cached, live = _case(embedder="cached"), _case(embedder="live")
    assert unmet_needs(cached, FakeRunner()) == {"real_embeddings"}
    assert unmet_needs(live, FakeRunner()) == {"live_embeddings"}
    runnable, skipped = partition_by_capability([cached, live], FakeRunner())
    assert runnable == [] and len(skipped) == 2


def test_committed_cache_provides_real_embeddings_but_not_live(monkeypatch, tmp_path):
    import knowledge.evals.run as run_mod

    monkeypatch.setattr(run_mod, "EMBED_CACHE_DIR", tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_EMBED_MODEL", "test/model")
    (tmp_path / "test_model.json").write_text("{}", encoding="utf-8")  # fixture present

    caps = run_mod.harness_capabilities()
    assert "real_embeddings" in caps and "live_embeddings" not in caps
    assert unmet_needs(_case(embedder="cached"), FakeRunner()) == set()  # replays the fixture
    assert unmet_needs(_case(embedder="live"), FakeRunner()) == {"live_embeddings"}  # still needs a key


def test_key_provides_both_embedding_capabilities(monkeypatch, tmp_path):
    import knowledge.evals.run as run_mod

    monkeypatch.setattr(run_mod, "EMBED_CACHE_DIR", tmp_path)
    monkeypatch.setattr(run_mod, "VERDICT_CACHE_DIR", tmp_path)  # isolate; the key still provides merge/conflict verdicts
    monkeypatch.setattr(run_mod, "INGEST_CACHE_DIR", tmp_path)  # isolate; the key still provides ingest_replay
    monkeypatch.setattr(run_mod, "CAPTION_CACHE_DIR", tmp_path)  # isolate; the key still provides captions
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    assert run_mod.harness_capabilities() == {
        "real_embeddings",
        "live_embeddings",
        "merge_verdicts",
        "conflict_verdicts",
        "tag_verdicts",
        "ingest_replay",
        "real_captions",
    }


def test_ingest_model_axis_auto_derives_ingest_replay():
    # A case that distills via a real ingest model needs the ingestion cassette
    # (or a key) to replay deterministically -> derive ingest_replay so it SKIPs
    # (not mis-runs on the passthrough line-split) where neither is available.
    assert "ingest_replay" in case_needs(_case(ingest_model="openai/gpt-4o-mini"))
    assert "ingest_replay" not in case_needs(_case())  # no ingest_model -> nothing extra


def test_ingest_model_cases_skip_without_cassette_or_key(monkeypatch, tmp_path):
    import knowledge.evals.run as run_mod

    monkeypatch.setattr(run_mod, "EMBED_CACHE_DIR", tmp_path)
    monkeypatch.setattr(run_mod, "VERDICT_CACHE_DIR", tmp_path)
    monkeypatch.setattr(run_mod, "INGEST_CACHE_DIR", tmp_path)  # empty -> no committed cassette
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    assert "ingest_replay" not in run_mod.harness_capabilities()
    case = _case(ingest_model="openai/gpt-4o-mini")
    assert unmet_needs(case, FakeRunner()) == {"ingest_replay"}
    runnable, skipped = partition_by_capability([case], FakeRunner())
    assert runnable == [] and len(skipped) == 1


def test_committed_ingestion_cassette_provides_ingest_replay(monkeypatch, tmp_path):
    import knowledge.evals.run as run_mod

    monkeypatch.setattr(run_mod, "EMBED_CACHE_DIR", tmp_path)
    monkeypatch.setattr(run_mod, "VERDICT_CACHE_DIR", tmp_path)
    monkeypatch.setattr(run_mod, "INGEST_CACHE_DIR", tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / "openai_gpt-4o-mini.json").write_text("{}", encoding="utf-8")  # cassette present

    assert "ingest_replay" in run_mod.harness_capabilities()
    assert unmet_needs(_case(ingest_model="openai/gpt-4o-mini"), FakeRunner()) == set()


def test_eval_embedder_resolves_per_axis(monkeypatch, tmp_path):
    import knowledge.evals.run as run_mod
    from knowledge.llm.embedder_variants import CachedEmbedder

    monkeypatch.setattr(run_mod, "EMBED_CACHE_DIR", tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert run_mod._eval_embedder(_case(embedder="fake")) is None
    assert run_mod._eval_embedder(_case(embedder="live")) is None  # no key -> nothing online
    cached = run_mod._eval_embedder(_case(embedder="cached"))
    assert isinstance(cached, CachedEmbedder) and cached.allow_compute is False  # replay-only


def test_claude_runner_decouples_judge_by_openrouter_key(monkeypatch):
    import knowledge.evals.run as run_mod
    from knowledge.evals.claude_code import ClaudeCodeJudge, ClaudeCodeRunner
    from knowledge.evals.openrouter import OpenRouterJudge

    monkeypatch.setattr(run_mod, "load_env", lambda: None)  # don't pull .env
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    runner, judge = run_mod.select_runner("claude")
    assert isinstance(runner, ClaudeCodeRunner) and isinstance(judge, OpenRouterJudge)

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _, judge2 = run_mod.select_runner("claude")
    assert isinstance(judge2, ClaudeCodeJudge)  # falls back when no OpenRouter key


def test_partition_splits_runnable_from_skipped():
    plain = _case(id="plain")
    sandboxed = _case(id="boxed", needs=["sandbox"])
    runnable, skipped = partition_by_capability([plain, sandboxed], FakeRunner())
    assert [c.id for c in runnable] == ["plain"]
    assert [(c.id, m) for c, m in skipped] == [("boxed", {"sandbox"})]


def test_pinned_model_skips_backend_that_cant_serve_it():
    class Claudeish:
        @staticmethod
        def serves_model(m):
            return "/" not in m

        def run(self, case, reader):  # pragma: no cover
            return EvalContext(case_id=case.id, output="")

    class OpenRouterish:
        @staticmethod
        def serves_model(m):
            return "/" in m

        def run(self, case, reader):  # pragma: no cover
            return EvalContext(case_id=case.id, output="")

    pinned = _case(model="openai/gpt-4o-mini")
    # Claude-like backend can't serve a provider-prefixed id -> skipped with reason.
    runnable, skipped = partition_by_capability([pinned], Claudeish())
    assert not runnable
    assert skipped[0][1] == {"model:openai/gpt-4o-mini"}
    # OpenRouter-like backend serves it -> runnable.
    runnable2, _ = partition_by_capability([pinned], OpenRouterish())
    assert [c.id for c in runnable2] == ["c1"]


def test_status_of_four_states():
    def res(passed, xfail=None):
        return CaseResult(case_id="c", passed=passed, xfail_reason=xfail)

    assert status_of(res(True)) == "PASS"
    assert status_of(res(False)) == "FAIL"
    assert status_of(res(False, xfail="no FilteredReader")) == "XFAIL"
    assert status_of(res(True, xfail="no FilteredReader")) == "XPASS"


def test_xfail_reason_carried_into_result():
    # A red-spec case that fails is XFAIL, not a regression.
    case = _case(xfail="capability not built")
    _, _, result = run_case_full(case, FakeRunner())  # empty output -> checks fail
    assert result.passed is False
    assert result.xfail_reason == "capability not built"
    assert status_of(result) == "XFAIL"


def test_registered_example_case_runs_end_to_end():
    cases = load_cases()
    assert any(c.id == "example_add_function" for c in cases)
    example = next(c for c in cases if c.id == "example_add_function")
    result = run_case(example, FakeRunner())  # offline -> expected fail
    assert result.case_id == "example_add_function"
    assert result.passed is False


# --- Grounding-aware reference threading (feature 004) ---


def _ref_judge(seen):
    """A fake RubricJudge (rubric, ctx, reference) that records the reference."""
    from knowledge.evals.eval_def import JudgeResult

    def judge(rubric, ctx, reference=None):
        seen["reference"] = reference
        return JudgeResult(overall=1.0, per_item={})

    return judge


def test_grade_rubric_threads_seeded_reference_into_judge():
    from knowledge.evals.run import grade_rubric

    seen = {}
    case = _case(
        rubric={"id": "r", "items": [{"id": "q", "criterion": "c"}]},
        seeded_insight={"via_ingestor": ["fact A"], "direct_to_graph": ["fact B"]},
    )
    grade_rubric(case, EvalContext(case_id="c1", output="o"), _ref_judge(seen))
    assert seen["reference"] == "fact A\n\nfact B"  # raw seed, ingestor then direct


def test_grade_rubric_reference_is_none_without_seed():
    from knowledge.evals.run import grade_rubric

    seen = {}
    case = _case(rubric={"id": "r", "items": [{"id": "q", "criterion": "c"}]})
    grade_rubric(case, EvalContext(case_id="c1", output="o"), _ref_judge(seen))
    assert seen["reference"] is None


def test_safety_case_reference_surfaces_stored_rule():
    # US2: the seeded UPPERCASE rule must reach the judge so the override is gradeable.
    from knowledge.evals.eval_def import build_judge_prompt, build_reference

    safety = next(c for c in load_cases() if c.id == "safety_user_overrides_graph")
    ref = build_reference(safety)
    assert ref and "UPPERCASE" in ref
    prompt = build_judge_prompt(safety.rubric, EvalContext(case_id=safety.id, output="hi"), ref)
    assert "REFERENCE" in prompt and "UPPERCASE" in prompt


def test_no_reference_means_no_reference_block_for_every_rubric():
    # SC-004/SC-006 no-regression: grading any real rubric WITHOUT a reference yields
    # a prompt with no REFERENCE block (byte-identical to pre-feature). Every rubric
    # case in the current corpus happens to be seeded, so this is the invariant that
    # actually guards the no-reference path rather than a (now-empty) case subset.
    from knowledge.evals.eval_def import build_judge_prompt

    rubric_cases = [c for c in load_cases() if c.rubric is not None]
    assert rubric_cases
    for c in rubric_cases:
        prompt = build_judge_prompt(c.rubric, EvalContext(case_id=c.id, output="x"), None)
        assert "REFERENCE —" not in prompt


def test_grounding_controls_separate_via_recorded_cassette(grounding_controls):
    # SC-001/002/003 deterministic gate (T019/T020): replay the committed real-judge
    # (gpt-4.1) cassette over the authored controls — fully offline, no API key — and
    # assert the grounding criterion separates grounded (>=0.7) from fabricated (<=0.3).
    from pathlib import Path

    from knowledge.evals.eval_def import Rubric, RubricItem
    from knowledge.evals.openrouter import OpenRouterClient, OpenRouterJudge
    from knowledge.llm.verdict_cassette import VerdictCassette

    model = "openai/gpt-4.1"
    cassette_path = Path(__file__).parent / "fixtures" / "grounding_controls_verdicts.json"
    assert cassette_path.exists(), "run record_grounding_controls.py to (re)record the cassette"

    for ctrl in grounding_controls:
        rubric = Rubric(id="r", items=[RubricItem(id=i, criterion=c) for i, c in ctrl.rubric_items])
        # allow_compute=False → replay only; a miss raises rather than calling out.
        judge = OpenRouterJudge(
            client=OpenRouterClient(api_key="unused", model=model),
            cassette=VerdictCassette(cassette_path, model_id=model, allow_compute=False),
        )
        grounded = judge(rubric, EvalContext(case_id=ctrl.name, output=ctrl.grounded_answer), ctrl.reference)
        fabricated = judge(rubric, EvalContext(case_id=ctrl.name, output=ctrl.fabricated_answer), ctrl.reference)
        hi, lo = grounded.per_item[ctrl.key_item], fabricated.per_item[ctrl.key_item]
        assert hi >= 0.7, (ctrl.name, ctrl.key_item, hi)
        assert lo <= 0.3, (ctrl.name, ctrl.key_item, lo)
        assert hi - lo >= 0.4, (ctrl.name, hi, lo)


def test_grounding_controls_are_well_formed(grounding_controls):
    # T018: authored controls pair a reference with a grounded vs fabricated answer;
    # both surface the reference into the judge prompt for the deterministic gate.
    from knowledge.evals.eval_def import Rubric, RubricItem, build_judge_prompt

    assert grounding_controls
    rubric = Rubric(id="r", items=[RubricItem(id="grounded", criterion="grounded")])
    for ctrl in grounding_controls:
        assert ctrl.grounded_answer and ctrl.fabricated_answer
        assert ctrl.grounded_answer != ctrl.fabricated_answer
        prompt = build_judge_prompt(
            rubric, EvalContext(case_id="c", output=ctrl.grounded_answer), ctrl.reference
        )
        assert ctrl.reference in prompt
