"""Unit tests for the planner-under-test (evals/plan_repro/planner.py).

No network: a fake ``Complete`` (prompt -> canned text) is injected. Exercises PRD loading,
prompt construction (incl. the checklist knob), tolerant parsing, and the save/reload round
trip with the coverage loader.
"""

from evals.plan_repro.coverage import load_candidate
from evals.plan_repro.planner import (
    build_planner_prompt,
    load_prd,
    parse_candidate,
    produce_candidate,
    save_candidate,
)

# The real checklist lives in Praxis (loaded at runtime); tests use a local sample so they
# exercise the injection without depending on Praxis or any baked-in list.
SAMPLE_CHECKLIST = [
    "Authentication completeness: include credential recovery (password reset).",
    "Every screen needs loading, empty, and error states.",
]


# --- PRD loading ---------------------------------------------------------------


def test_load_prd_reads_inspiration_docs():
    prd = load_prd()
    assert len(prd) > 1000
    low = prd.lower()
    assert "daily" in low and "invite" in low  # tokens from the real PRD set
    # The raw PRD genuinely lacks password reset — that's the eval's whole point.
    assert "password reset" not in low


# --- prompt --------------------------------------------------------------------


def test_planner_prompt_baseline_has_no_lenses():
    prompt = build_planner_prompt("PRD BODY")
    assert "PRD BODY" in prompt
    assert "JSON only" in prompt
    assert "general engineering considerations" not in prompt


def test_planner_prompt_treatment_injects_checklist():
    prompt = build_planner_prompt("PRD BODY", checklist=SAMPLE_CHECKLIST)
    assert "general engineering considerations" in prompt
    assert "credential recovery" in prompt  # a lens, not the golden feature itself


# --- parsing -------------------------------------------------------------------


def test_parse_array_of_objects():
    feats = parse_candidate('[{"id":"R1","text":"feature one"},{"id":"R2","text":"feature two"}]')
    assert [f.id for f in feats] == ["R1", "R2"]
    assert feats[0].text == "feature one"


def test_parse_fenced_array():
    feats = parse_candidate('```json\n[{"text":"only one"}]\n```')
    assert len(feats) == 1
    assert feats[0].text == "only one"
    assert feats[0].id == "C0"  # generated when absent


def test_parse_dict_with_features_key():
    feats = parse_candidate({"features": [{"id": "X", "text": "a"}, {"id": "Y", "text": "b"}]})
    assert [f.id for f in feats] == ["X", "Y"]


def test_parse_list_of_strings():
    feats = parse_candidate('["alpha","beta"]')
    assert [f.text for f in feats] == ["alpha", "beta"]


def test_parse_prose_wrapped_and_drops_empty_text():
    feats = parse_candidate('Here you go: [{"text":"kept"},{"text":""}] done')
    assert [f.text for f in feats] == ["kept"]  # empty-text item dropped


def test_parse_garbage_is_empty():
    assert parse_candidate("no json here") == []


# --- produce + persist ---------------------------------------------------------


def test_produce_candidate_with_fake_model():
    complete = lambda prompt: '[{"id":"R1","text":"a user can sign up"}]'
    feats = produce_candidate(complete, "PRD")
    assert len(feats) == 1 and feats[0].text == "a user can sign up"


def test_produce_candidate_passes_checklist_through():
    seen = {}

    def complete(prompt):
        seen["prompt"] = prompt
        return '[]'

    produce_candidate(complete, "PRD", checklist=SAMPLE_CHECKLIST)
    assert "credential recovery" in seen["prompt"]


def test_save_candidate_round_trips_through_loader(tmp_path):
    feats = parse_candidate('[{"id":"R1","text":"feature one"},{"id":"R2","text":"feature two"}]')
    out = tmp_path / "candidate.yaml"
    save_candidate(feats, out, project="team-app")
    reloaded = load_candidate(out)
    assert [f.id for f in reloaded] == ["R1", "R2"]
    assert [f.text for f in reloaded] == ["feature one", "feature two"]
