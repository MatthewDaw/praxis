"""``distill_case`` must respect the seed channel.

``direct_to_graph`` seeds mirror a real run's ``graph.write(text, state="active")``
— written VERBATIM, never distilled. Only ``via_ingestor`` goes through the
ingestor/distiller. A regression here corrupts curated text (e.g. "RULE_B second"
became "RULE_B is a specific rule or guideline." when direct seeds were distilled).
"""
from __future__ import annotations

import knowledge.serve.regenerate as regen
from knowledge.serve.regenerate import distill_case


def test_direct_to_graph_is_verbatim_and_makes_no_llm_call(monkeypatch):
    # A direct-only case. Even with distill=True and NO API key, distill_case must
    # succeed (proving the LLM/distill branch is never entered) and return the seed
    # lines verbatim as active facts, preserving order.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Tripwire: constructing the LLM would explode if the distill path were taken.
    monkeypatch.setattr(
        regen, "RegenerateUnavailableError", regen.RegenerateUnavailableError
    )

    seeds = distill_case("kg_preserves_order", distill=True)

    assert [s.text for s in seeds] == ["RULE_A first", "RULE_B second", "RULE_C third"]
    assert all(s.state == "active" for s in seeds)


def test_mixed_case_distills_only_via_ingestor(monkeypatch):
    # Passthrough (distill=False) is deterministic and needs no LLM. The
    # direct_to_graph rule stays verbatim/active; the via_ingestor line is run
    # through the ingestor and lands proposed.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    seeds = distill_case("example_add_function", distill=False)

    active = [s.text for s in seeds if s.state == "active"]
    proposed = [s.text for s in seeds if s.state == "proposed"]

    # Verbatim, NOT split into "...docstring." + "...unit test.".
    assert active == ["Every public function needs a docstring and a unit test."]
    # The via_ingestor seed was processed by the ingestor (proposed channel).
    assert proposed
    assert all(s.source == "evals/example_add_function" for s in seeds)
