"""Full-chain reproduction of the source/scope generation-drift escape (R-HAS-SOURCE).

The original failure was NOT "the gate forgot to check source" in the abstract — it was
a three-link silent build-pass:

  (1) ADMIT: a planning run emitted a requirement tagged top-level scope="team-app" with
      NO source="prd-team-app". The gate's other rules (R-ACCEPT-BINARY / R-NO-VAGUE /
      R-NO-DANGLING) do not look at ``source``, so nothing flagged it and the plan was
      admitted.
  (2) SILENT-EMPTY: the downstream completeness contract — Praxis
      ``incomplete_requirements`` — filters category=="requirement" AND
      source==f"prd-{project}". A scope-tagged-but-source-less requirement is filtered
      out, so the query returns EMPTY for that plan.
  (3) SILENT BUILD-PASS: the build-completeness Stop hook sees incompleteCount==0 and
      declares the build DONE with nothing actually built.

The existing eval case (plan_gate_missing_project_source_rejected) only proves link (3)
of the *fix* — that the guardrail now fires. This test reproduces the original failure
chain itself: it proves nothing else caught the drift (a), makes the silent-empty
completeness consequence explicit (b), and only then confirms the guardrail closes it (c).
"""

from __future__ import annotations

from agent_factory.plan_gate import (
    R_ACCEPT_BINARY,
    R_HAS_SOURCE,
    R_NO_DANGLING,
    R_NO_VAGUE,
    Requirement,
    evaluate_plan,
)

PROJECT = "team-app"


# --- the REAL plan's fact shape ------------------------------------------------
# This is the Praxis fact as the drifted planning run actually wrote it: scope is the
# top-level "team-app" tag, mvp/verify/requirement_id live under meta, acceptance is
# valid binary prose — and there is NO top-level ``source="prd-team-app"``. That single
# missing tag is the whole bug.
DRIFTED_REQUIREMENT_FACT = {
    "category": "requirement",
    "scope": "team-app",  # top-level scope tag — looks plausible, but it is NOT a source
    "text": (
        "Participation% for date D = active athletes with completed=true "
        "/ active athletes on roster * 100."
    ),
    "acceptance": (
        "with N active and K completed, pct = round(K/N*100); "
        "N=0 -> None (no active roster), never 0."
    ),
    "meta": {
        "scope": "mvp",
        "verify": "automated",
        "requirement_id": "R13",
    },
    # NOTE: no "source" key at all — the generation drift.
}


def _fact_to_requirement(fact: dict) -> Requirement:
    """Map the raw Praxis fact into the gate's ``Requirement`` exactly as the plan skill
    would have — crucially carrying the *absent* top-level source through unchanged."""
    return Requirement(
        id=fact["meta"]["requirement_id"],
        text=fact["text"],
        acceptance=fact["acceptance"],
        defines=fact.get("defines", []),
        references=fact.get("references", []),
        source=fact.get("source", ""),  # absent -> "" , the drift the gate must catch
    )


def _incomplete_requirements(facts: list[dict], project: str) -> list[dict]:
    """A faithful, pure local model of the Praxis ``incomplete_requirements`` completeness
    filter: a fact is an in-scope requirement only when category=="requirement" AND its
    top-level source equals f"prd-{project}". Anything else (including a scope-tagged,
    source-less requirement) is filtered out and silently never appears in the
    incomplete set."""
    want_source = f"prd-{project}"
    return [
        f
        for f in facts
        if f.get("category") == "requirement" and f.get("source") == want_source
    ]


def test_full_chain_source_scope_drift():
    req = _fact_to_requirement(DRIFTED_REQUIREMENT_FACT)

    # (a) GAP EXISTED — nothing ELSE caught it. The drifted requirement has a valid
    #     binary acceptance, no vague terms, and no dangling references, so the gate's
    #     OTHER rules stay silent. Pre-guardrail, the ONLY thing wrong is the missing
    #     source — which those rules don't inspect — so the plan would have been ADMITTED.
    verdict = evaluate_plan([req], project=PROJECT)
    fired = set(verdict.rule_ids)
    assert R_ACCEPT_BINARY not in fired, "acceptance is valid; this rule must not fire"
    assert R_NO_VAGUE not in fired, "no vague terms; this rule must not fire"
    assert R_NO_DANGLING not in fired, "no dangling refs; this rule must not fire"
    # Prove the admit-path concretely: strip out the new guardrail's reasons and what
    # remains is an EMPTY reason list — i.e. the pre-R-HAS-SOURCE gate admits this plan.
    pre_guardrail_reasons = [r for r in verdict.reasons if r.rule_id != R_HAS_SOURCE]
    assert pre_guardrail_reasons == [], (
        "before R-HAS-SOURCE existed, the gate had no reason to reject — it ADMITTED "
        f"the drifted plan: {pre_guardrail_reasons}"
    )

    # (b) SILENT-EMPTY — the downstream completeness query returns []. Because the
    #     admitted requirement carries scope="team-app" but no source="prd-team-app",
    #     the completeness filter excludes it entirely. The build therefore sees an
    #     empty incomplete set...
    incomplete = _incomplete_requirements([DRIFTED_REQUIREMENT_FACT], PROJECT)
    assert incomplete == [], (
        "the completeness filter must drop the scope-tagged/source-less requirement — "
        "this empty result is the silent build-pass"
    )
    incomplete_count = len(incomplete)
    assert incomplete_count == 0, (
        "incompleteCount==0 is exactly what the build-completeness Stop hook reads as "
        "DONE — the build declares success with R13 never built"
    )
    # Sanity anchor: the SAME fact, correctly tagged, WOULD have been counted — proving
    # the empty result is caused by the missing source, not by the filter shape.
    correctly_tagged = {**DRIFTED_REQUIREMENT_FACT, "source": f"prd-{PROJECT}"}
    assert _incomplete_requirements([correctly_tagged], PROJECT) == [correctly_tagged]

    # (c) FIX CLOSES IT — the guardrail now REJECTS, naming the expected source, so the
    #     drifted plan can never be admitted and the silent-empty chain (b) is unreachable.
    assert not verdict.admitted, "R-HAS-SOURCE must reject the drifted plan"
    assert R_HAS_SOURCE in fired
    assert any(
        r.rule_id == R_HAS_SOURCE and f"prd-{PROJECT}" in r.message
        for r in verdict.reasons
    ), "the rejection must name the expected prd-team-app source"
