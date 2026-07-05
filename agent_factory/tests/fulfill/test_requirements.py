"""U4 — requirement model, cover resolution, and Δ-ranking tests."""

from __future__ import annotations

from agent_factory.fulfill.requirements import (
    ASK,
    DEFAULT,
    TRIAGE,
    WAIT,
    FulfillRequirement,
    materiality,
    rank_open,
    requirement_from_fact,
    resolve_cover,
)


def _open_reqs(domain):
    """All six pack requirements as typed FulfillRequirements (the seeded-open set)."""
    return [requirement_from_fact(r) for r in domain.requirements]


def test_requirement_from_meta_shape(domain):
    fact = {
        "id": "cid-123",
        "meta": {
            "requirement_id": "T1",
            "field": "filing_status",
            "verify": "schema_valid",
            "cover": ["user", "default:filing_status"],
            "renders": ["line_12_standard_deduction"],
        },
    }
    req = requirement_from_fact(fact)
    assert req.id == "T1"
    assert req.field == "filing_status"
    assert req.cover == ["user", "default:filing_status"]
    assert req.fact_id == "cid-123"


def test_requirement_from_raw_pack_dict(domain):
    req = requirement_from_fact(domain.requirement("T2"))
    assert req.id == "T2"
    assert req.field == "box1_wages"
    assert req.cover == ["document:w2", "user"]


def test_filing_status_is_most_material(domain):
    facts = {"box1_wages": 40000, "box2_withholding": 3200}
    ranked = rank_open(_open_reqs(domain), domain, facts)
    asks = [r for r in ranked if r.disposition == ASK]
    # filing_status (T1) carries the most bottom-line swing of any open requirement.
    assert asks[0].req.id == "T1"
    assert asks[0].materiality > 100


def test_other_income_is_default_not_ask(domain):
    facts = {"box1_wages": 40000, "box2_withholding": 3200}
    ranked = {r.req.id: r for r in rank_open(_open_reqs(domain), domain, facts)}
    assert ranked["T5"].disposition == DEFAULT  # other_income: near-zero materiality
    assert ranked["T5"].materiality < 1.0


def test_resolve_cover_document_when_w2_present(domain):
    req = requirement_from_fact(domain.requirement("T2"))  # box1_wages, cover [document:w2, user]
    assert resolve_cover(req, {"box1_wages": 40000}, domain) == "document:w2"
    assert resolve_cover(req, {}, domain) == "ask"


def test_resolve_cover_ask_then_default(domain):
    req = requirement_from_fact(domain.requirement("T1"))  # filing_status, cover [user, default:..]
    assert resolve_cover(req, {}, domain) == "ask"


def test_guard_absent_signal_not_asked(domain):
    facts = {"box1_wages": 40000, "box2_withholding": 3200}
    ranked = {r.req.id: r for r in rank_open(_open_reqs(domain), domain, facts)}
    # T6 (dependents) guarded by has_dependents_signal == true; absent -> defaulted, never asked.
    assert ranked["T6"].disposition == DEFAULT


def test_depends_on_unmet_waits(domain):
    # T4 (confirmed_w2) depends on T2/T3; with neither value present it is not yet askable and has
    # no default -> WAIT.
    ranked = {r.req.id: r for r in rank_open(_open_reqs(domain), domain, {})}
    assert ranked["T4"].disposition == WAIT


def test_depends_on_met_becomes_askable(domain):
    facts = {"box1_wages": 40000, "box2_withholding": 3200}
    ranked = {r.req.id: r for r in rank_open(_open_reqs(domain), domain, facts)}
    assert ranked["T4"].disposition == ASK  # readback now makes sense


def test_fieldless_fact_routes_to_triage(domain):
    bad = FulfillRequirement(id="TX", field="")
    ranked = rank_open([bad], domain, {})
    assert ranked[0].disposition == TRIAGE


def test_materiality_zero_for_single_candidate(domain):
    req = requirement_from_fact(domain.requirement("T5"))  # other_income, default 0 only
    assert materiality(domain, req, {"box1_wages": 40000}) == 0.0
