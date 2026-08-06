"""Locks item 3: the MECHANICAL plan gate (``agent_factory/tools/plan_gate_check.py``).

The tool reads the LIVE ``prd-<project>`` requirement facts, maps each onto a plan-gate
:class:`Requirement`, runs ``evaluate_plan``, and exits 0=admitted / 1=rejected / 2=cannot-run
(Praxis unreachable OR an empty plan — never a vacuous PASS).

We monkeypatch ``pgc._praxis`` with a fake whose ``facts_by`` (a) ASSERTS it is queried with
``category="requirement", space=<bare>, snapshot="prd-<bare>"`` and (b) returns canned requirement
facts, so the whole read→map→evaluate→exit-code path is asserted deterministically with no network.
"""

import sys
from pathlib import Path

import pytest

_TOOLS = str(Path(__file__).resolve().parent.parent / "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import plan_gate_check as pgc  # noqa: E402


# A signed contract episode (evaluator recorded real cut/merge/add actions). Threaded by default so
# these tests exercise the OTHER (decision/source/dep) rules — R-CONTRACT-SIGNED is locked separately
# in test_plan_gate_contract.py. ``episodes=None`` here means "the default signed contract".
_SIGNED_EPISODE = {
    "id": "ep-1",
    "meta": {"episode": {"kind": "contract-signed", "n_assertions": 14,
                         "actions": {"cut": 3, "merged": 1, "added": 2}, "signer": "evaluator"}},
}


class FakePraxis:
    """Fake exposing the methods the tool calls. ``facts_by`` asserts the exact live-plan queries
    (category/space/snapshot) then returns canned facts — or raises, to model Praxis being down.

    The tool makes TWO reads over the same ``(space, snapshot)``: the CATEGORICAL one
    (``category="requirement"``, default active state) and the BROADER divergence read
    (``category=None, state="any"``) that R-NO-INVISIBLE-TICKET / R-REJECTED-WITHOUT-AUDIT need.
    ``all_facts`` is what the broader read returns; it defaults to the categorical facts, i.e. a
    snapshot where write and read agree and nothing is rejected.

    ``get_episodes`` returns the signed-contract episode (default) so the contract rule passes."""

    def __init__(self, facts=None, raise_exc=None, episodes=None, all_facts=None):
        self._facts = [] if facts is None else list(facts)
        self._all = self._facts if all_facts is None else list(all_facts)
        self._raise = raise_exc
        self._episodes = [_SIGNED_EPISODE] if episodes is None else list(episodes)
        self.calls = []

    def facts_by(self, category=None, space=None, snapshot=None, state="active", **k):
        self.calls.append((category, space, snapshot, state))
        # both reads target the project's prd-<bare> snapshot.
        assert space == "sotos"
        assert snapshot == "prd-sotos"
        if self._raise is not None:
            raise self._raise
        if category is None:
            # the broader divergence read MUST span every lifecycle state, or a rejected ticket
            # could never be seen.
            assert state == "any"
            return list(self._all)
        assert category == "requirement"
        return list(self._facts)

    def get_episodes(self, *, meta=None, space=None, snapshot=None):
        if self._raise is not None:
            raise self._raise
        return list(self._episodes)


def _fact(rid, *, acceptance, tags, verify, decision="", depends_on=None, source="prd-sotos"):
    return {
        "id": rid,
        "text": f"{rid} does its thing",
        "source": source,
        "meta": {
            "requirement_id": rid,
            "acceptance": acceptance,
            "tags": list(tags),
            "verify": verify,
            "decision": decision,
            "depends_on": list(depends_on or []),
        },
    }


# A MALFORMED plan: an IMPL-tagged decision (tags ["cdk","cognito"], NO architecture-decision tag)
# recognized ONLY by its meta.decision marker, verify=automated (an impl end-state), and an impl
# ticket R1 that depends_on it. Both decision rules must fire.
def _malformed():
    return [
        _fact("D1", acceptance="the cognito pool is provisioned", tags=["cdk", "cognito"],
              verify="automated", decision="human-decided"),
        _fact("R1", acceptance="login works end to end", tags=["impl"], verify="automated",
              depends_on=["D1"]),
    ]


# A WELL-FORMED plan: D1 is a manual, decision-level acceptance decision NOTHING depends on; R1 is a
# normal impl ticket depending on nothing. Every mechanical rule passes.
def _wellformed():
    return [
        _fact("D1", acceptance="the team accepts the cdk + cognito design", tags=["cdk", "cognito"],
              verify="manual", decision="human-decided"),
        _fact("R1", acceptance="login works end to end", tags=["impl"], verify="automated"),
    ]


def _install(monkeypatch, **kw):
    fake = FakePraxis(**kw)
    monkeypatch.setattr(pgc, "_praxis", fake)
    return fake


# --------------------------------------------------------------------------- read query + malformed

def test_facts_read_from_prd_snapshot_and_malformed_plan_rejects(monkeypatch):
    fake = _install(monkeypatch, facts=_malformed())
    verdict, requirements = pgc.check_plan("sotos")
    # the fake ASSERTED (category, space, snapshot); confirm BOTH lanes were actually queried.
    # Three reads, all over this project's own space: the categorical requirement enumeration the
    # pure rules run on, the broader any-state one the live-data rules need, and the declared
    # build-validation checks threaded in for R-EXTERNAL-STATE-NEEDS-LIVE-CHECK (the pure gate still
    # reads nothing itself — the caller fetches, exactly as it does for the signed contract).
    assert fake.calls == [("requirement", "sotos", "prd-sotos", "active"),
                          (None, "sotos", "prd-sotos", "any"),
                          ("check", "sotos", "building-validation", "active")]
    assert len(requirements) == 2
    assert verdict.admitted is False
    rule_ids = {r.rule_id for r in verdict.reasons}
    assert "R-DECISION-NOT-END-STATE" in rule_ids
    assert "R-NO-IMPL-DEPENDS-ON-DECISION" in rule_ids


def test_main_rejects_malformed_plan_and_prints_reasons_to_stderr(monkeypatch, capsys):
    _install(monkeypatch, facts=_malformed())
    rc = pgc.main(["sotos"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "R-DECISION-NOT-END-STATE" in err
    assert "R-NO-IMPL-DEPENDS-ON-DECISION" in err


# --------------------------------------------------------------------------- well-formed admits

def test_wellformed_plan_admits(monkeypatch):
    _install(monkeypatch, facts=_wellformed())
    verdict, requirements = pgc.check_plan("sotos")
    assert verdict.admitted is True
    assert verdict.reasons == []


def test_main_admits_wellformed_plan(monkeypatch, capsys):
    _install(monkeypatch, facts=_wellformed())
    rc = pgc.main(["sotos"])
    assert rc == 0
    assert capsys.readouterr().err == ""


# --------------------------------------------------------------------------- no vacuous pass

def test_zero_facts_raises_valueerror_and_main_returns_2(monkeypatch, capsys):
    _install(monkeypatch, facts=[])
    with pytest.raises(ValueError):
        pgc.check_plan("sotos")
    # main must NOT report a vacuous PASS: exit 2, error on stderr.
    _install(monkeypatch, facts=[])
    rc = pgc.main(["sotos"])
    assert rc == 2
    assert "error" in capsys.readouterr().err.lower()


# --------------------------------------------------------------------------- Praxis unreachable

def test_praxis_unreachable_main_returns_2(monkeypatch, capsys):
    _install(monkeypatch, raise_exc=pgc.PraxisUnreachable("connection refused"))
    rc = pgc.main(["sotos"])
    assert rc == 2
    assert "unreachable" in capsys.readouterr().err.lower()


# =========================================================================== live-data rules
#
# The three rules below reproduce the prd-sotos corruption the gate MISSED: a duplicate identity, a
# batch of tickets written with a NULL category (structurally invisible to the categorical read), and
# already-hardened tickets flipped to rejected with no human in the loop. Each fires on data the pure
# rules pass, so each fails against the pre-change tool.


def _ticket(rid, *, state="active", category="requirement", fid=None, build_state="never-built",
            audit=None, source="prd-sotos"):
    """A live FACT as ``facts_by`` returns it (``state``/``category`` are top-level columns)."""
    fact = _fact(rid, acceptance=f"{rid} works end to end", tags=["impl"], verify="automated",
                 source=source)
    fact["id"] = fid or rid
    fact["state"] = state
    fact["category"] = category
    fact["meta"]["build_state"] = build_state
    if audit is not None:
        fact["meta"]["auditTrail"] = audit
    return fact


# --------------------------------------------------------------------------- R-NO-DUPLICATE-REQUIREMENT-ID

def test_duplicate_requirement_id_across_active_facts_rejects(monkeypatch):
    """CHAT14 x2: two ACTIVE facts wearing one requirement_id. The pure gate collapses them into one
    id-keyed Requirement and admits; the live rule must name BOTH fact ids and the requirement_id."""
    dupes = [_ticket("CHAT14", fid="f-a"), _ticket("CHAT14", fid="f-b"), _ticket("CHAT2", fid="f-c")]
    _install(monkeypatch, facts=dupes)
    verdict, _ = pgc.check_plan("sotos")
    assert verdict.admitted is False
    hits = [r for r in verdict.reasons if r.rule_id == pgc.R_NO_DUPLICATE_REQUIREMENT_ID]
    assert len(hits) == 1
    assert "CHAT14" in hits[0].message
    assert "f-a" in hits[0].message and "f-b" in hits[0].message
    assert "CHAT2" not in hits[0].message


def test_duplicate_requirement_id_main_exits_1(monkeypatch, capsys):
    _install(monkeypatch, facts=[_ticket("CHAT14", fid="f-a"), _ticket("CHAT14", fid="f-b")])
    assert pgc.main(["sotos"]) == 1
    assert pgc.R_NO_DUPLICATE_REQUIREMENT_ID in capsys.readouterr().err


def test_duplicate_rule_ignores_non_active_twin(monkeypatch):
    """A retired (rejected) twin is NOT an identity collision — only ACTIVE facts contend."""
    _install(monkeypatch, facts=[_ticket("CHAT14", fid="f-a")],
             all_facts=[_ticket("CHAT14", fid="f-a"),
                        _ticket("CHAT14", fid="f-old", state="rejected",
                                audit=[{"action": "rejected", "actor": "human-gate"}])])
    verdict, _ = pgc.check_plan("sotos")
    assert pgc.R_NO_DUPLICATE_REQUIREMENT_ID not in verdict.rule_ids


# --------------------------------------------------------------------------- R-NO-INVISIBLE-TICKET

def test_invisible_null_category_tickets_reject_even_when_unreferenced(monkeypatch):
    """The incident shape: CHAT7/CHAT11 were written with a NULL category and only surfaced
    INDIRECTLY (via R-NO-DANGLING-DEP on the tickets depending on them), while CHAT9/CHAT15/CHAT16
    were equally invisible and drew NOTHING. The divergence itself must be the error."""
    visible = [_ticket("CHAT1")]
    invisible = [_ticket(r, category=None) for r in ("CHAT7", "CHAT9", "CHAT15")]
    _install(monkeypatch, facts=visible, all_facts=visible + invisible)
    verdict, _ = pgc.check_plan("sotos")
    assert verdict.admitted is False
    hits = [r for r in verdict.reasons if r.rule_id == pgc.R_NO_INVISIBLE_TICKET]
    assert len(hits) == 1
    msg = hits[0].message
    # every id named, including the UNREFERENCED ones nothing depends_on.
    for rid in ("CHAT7", "CHAT9", "CHAT15"):
        assert rid in msg
    # and it says WHY: the category column is what blinds every reader.
    assert "category" in msg.lower()
    assert "RESOLVE" in msg or "incomplete_requirements" in msg


def test_invisible_ticket_main_exits_1(monkeypatch, capsys):
    visible = [_ticket("CHAT1")]
    _install(monkeypatch, facts=visible, all_facts=visible + [_ticket("CHAT16", category=None)])
    assert pgc.main(["sotos"]) == 1
    err = capsys.readouterr().err
    assert pgc.R_NO_INVISIBLE_TICKET in err and "CHAT16" in err


def test_invisible_rule_does_not_fire_on_non_ticket_facts(monkeypatch):
    """A plan snapshot also holds planning markers, episodes and surfaces. Those carry NEITHER
    requirement_id NOR build_state, so a healthy plan must still exit 0."""
    visible = _wellformed()
    noise = [
        {"id": "marker-1", "state": "active", "category": "planning-marker",
         "source": "prd-sotos", "text": "planning marker", "meta": {"project": "sotos"}},
        {"id": "ep-9", "state": "active", "category": None, "source": "prd-sotos",
         "text": "an episode with no category at all", "meta": {}},
        {"id": "s-home", "state": "active", "category": "surface", "source": "prd-sotos",
         "text": "home screen", "meta": {"screen_id": "s-home"}},
    ]
    _install(monkeypatch, facts=visible, all_facts=visible + noise)
    verdict, _ = pgc.check_plan("sotos")
    assert verdict.reasons == []
    assert verdict.admitted is True


def test_invisible_rule_ignores_a_foreign_plans_ticket(monkeypatch):
    """A ticket whose provenance names ANOTHER plan (mounted/copied in) is not this plan's
    divergence."""
    visible = _wellformed()
    foreign = _ticket("OTHER1", category=None, source="prd-other-app")
    _install(monkeypatch, facts=visible, all_facts=visible + [foreign])
    verdict, _ = pgc.check_plan("sotos")
    assert verdict.admitted is True


# --------------------------------------------------------------------------- R-REJECTED-WITHOUT-AUDIT

def test_rejected_without_human_audit_warns_without_changing_exit_code(monkeypatch, capsys):
    """Three already-hardened tickets flipped to rejected with no human in the loop. A fact with NO
    auditTrail of its own is the fingerprint — fact_to_candidate SYNTHESIZES the pipeline
    distilled/scored pair at read time for exactly those, so actor=="pipeline" means "never carried a
    real trail". Loud, but advisory: a healthy plan still exits 0."""
    visible = _wellformed()
    silently_rejected = [
        _ticket("CHAT20", state="rejected"),                       # no auditTrail key at all
        _ticket("CHAT21", state="rejected", audit=[]),             # empty trail
        _ticket("CHAT22", state="rejected",                        # read-time synthesized only
                audit=[{"action": "distilled", "actor": "pipeline"},
                       {"action": "scored", "actor": "pipeline"}]),
    ]
    _install(monkeypatch, facts=visible, all_facts=visible + silently_rejected)
    verdict, _ = pgc.check_plan("sotos")
    assert verdict.admitted is True            # advisory only
    assert {w.rule_id for w in verdict.warnings} == {pgc.R_REJECTED_WITHOUT_AUDIT}
    assert {"CHAT20", "CHAT21", "CHAT22"} == {w.message.split()[0] for w in verdict.warnings}

    _install(monkeypatch, facts=visible, all_facts=visible + silently_rejected)
    assert pgc.main(["sotos"]) == 0            # exit-code semantics preserved
    err = capsys.readouterr().err
    assert pgc.R_REJECTED_WITHOUT_AUDIT in err
    for rid in ("CHAT20", "CHAT21", "CHAT22"):
        assert rid in err


def test_human_rejected_ticket_does_not_warn(monkeypatch):
    """A deliberate human rejection leaves action=rejected/actor=<non-pipeline> on the trail."""
    visible = _wellformed()
    audited = [
        _ticket("CHAT30", state="rejected",
                audit=[{"action": "created", "actor": "human-gate"},
                       {"action": "rejected", "actor": "human-gate", "note": "cut at intake"}]),
        _ticket("CHAT31", state="rejected",
                audit=[{"action": "superseded", "actor": "matt", "note": "merged into CHAT30"}]),
    ]
    _install(monkeypatch, facts=visible, all_facts=visible + audited)
    verdict, _ = pgc.check_plan("sotos")
    assert verdict.warnings == []
    assert verdict.admitted is True


# --------------------------------------------------------------------- absorbed-ticket carve-out
#
# The live prd-sotos false positive: OBS23 was flagged as an invisible ticket, but it was a correct
# deliberate closure — a review pass found it had no standalone acceptance, folded it into OBS18, and
# re-categorised it to "note" so no reader treats it as buildable. Re-categorising it IS how that
# closure is recorded, so the rule was demanding a "repair" that would destroy the decision.

def _absorbed(rid, *, into="OBS18", category="note", state="active"):
    """A ticket closed by absorption, shaped like the live OBS23."""
    fact = _ticket(rid, category=category, state=state, build_state="merged")
    fact["meta"]["merged_into"] = into
    fact["meta"]["merge_reason"] = (
        "B1c contract-signing evaluator: no standalone tie to any Key Flow; "
        f"{into}'s acceptance now absorbs it")
    return fact


def test_absorbed_ticket_does_not_fire_invisible_rule(monkeypatch, capsys):
    """OBS23 itself: merged + merged_into a ticket that exists + moved out of the requirement lane.
    Not lost work — a recorded closure. The plan must still exit 0."""
    visible = _wellformed() + [_ticket("OBS18")]
    _install(monkeypatch, facts=visible, all_facts=visible + [_absorbed("OBS23")])
    verdict, _ = pgc.check_plan("sotos")
    assert pgc.R_NO_INVISIBLE_TICKET not in verdict.rule_ids
    assert verdict.admitted is True

    _install(monkeypatch, facts=visible, all_facts=visible + [_absorbed("OBS23")])
    assert pgc.main(["sotos"]) == 0
    assert capsys.readouterr().err == ""


def test_merged_without_merged_into_still_fires(monkeypatch):
    """The carve-out must not become a blanket escape hatch: a bare ``merged`` with nothing to point
    at is an unfinished closure, indistinguishable from work that was simply dropped."""
    visible = _wellformed()
    orphan = _ticket("OBS23", category=None, build_state="merged")   # no merged_into
    _install(monkeypatch, facts=visible, all_facts=visible + [orphan])
    verdict, _ = pgc.check_plan("sotos")
    assert pgc.R_NO_INVISIBLE_TICKET in verdict.rule_ids
    assert verdict.admitted is False


def test_merged_into_a_ticket_that_does_not_exist_still_fires(monkeypatch):
    """Absorption into a survivor that is not in this plan is not absorption — it is the incident one
    level deeper: the work is gone and a stamped field stops anyone looking. Otherwise anything could
    dodge the rule by inventing a target."""
    visible = _wellformed()                      # NB: no OBS18 anywhere in the snapshot
    _install(monkeypatch, facts=visible, all_facts=visible + [_absorbed("OBS23", into="OBS18")])
    verdict, _ = pgc.check_plan("sotos")
    assert pgc.R_NO_INVISIBLE_TICKET in verdict.rule_ids
    assert verdict.admitted is False


def test_absorbed_into_a_fact_id_resolves(monkeypatch):
    """``merged_into`` is hand-written and may name the survivor's FACT id rather than its
    requirement_id; both namespaces resolve."""
    survivor = _ticket("OBS18", fid="764263fb788f4b18ace036ac6e154c87")
    visible = _wellformed() + [survivor]
    absorbed = _absorbed("OBS23", into="764263fb788f4b18ace036ac6e154c87")
    _install(monkeypatch, facts=visible, all_facts=visible + [absorbed])
    verdict, _ = pgc.check_plan("sotos")
    assert verdict.admitted is True


def test_category_less_ticket_with_no_merge_markers_still_fires(monkeypatch):
    """The original rule is intact: a genuinely category-less ticket carrying no merge markers is
    still the write/read divergence, carve-out or no carve-out."""
    visible = _wellformed()
    _install(monkeypatch, facts=visible, all_facts=visible + [_ticket("CHAT9", category=None)])
    verdict, _ = pgc.check_plan("sotos")
    assert pgc.R_NO_INVISIBLE_TICKET in verdict.rule_ids
    assert verdict.admitted is False


def test_absorbed_twin_is_not_a_duplicate_identity(monkeypatch):
    """R-NO-DUPLICATE-REQUIREMENT-ID needs NO absorption exemption, and must not have one.

    An absorbed OBS23 alongside a live requirement OBS23 is not ambiguity: the rule scopes to the
    requirement lane, which IS the set of facts an id-keyed read can return, and absorption moved the
    note out of it. Two ACTIVE facts *in* the lane stay a collision no matter what their meta says —
    asserted here too, so a merge marker can never be used to dodge the identity rule."""
    live_obs23 = _ticket("OBS23", fid="f-live")
    visible = _wellformed() + [_ticket("OBS18"), live_obs23]
    _install(monkeypatch, facts=visible, all_facts=visible + [_absorbed("OBS23")])
    verdict, _ = pgc.check_plan("sotos")
    assert verdict.admitted is True             # the note does not contend for the id

    # ...but a merge marker on a fact still IN the requirement lane does not excuse the collision.
    merged_but_in_lane = _absorbed("OBS23", category="requirement")
    merged_but_in_lane["id"] = "f-dupe"
    visible2 = _wellformed() + [_ticket("OBS18"), live_obs23, merged_but_in_lane]
    _install(monkeypatch, facts=visible2, all_facts=visible2)
    verdict, _ = pgc.check_plan("sotos")
    assert pgc.R_NO_DUPLICATE_REQUIREMENT_ID in verdict.rule_ids


def test_absorbed_ticket_does_not_trip_the_rejected_warning(monkeypatch):
    """Verified, not assumed: an absorbed ticket is ACTIVE so the warning never reaches it — and even
    when a closure is implemented as a REJECTION, merged_into/merge_reason is the human record, so it
    is not the silent auto-rejection the warning hunts."""
    visible = _wellformed() + [_ticket("OBS18")]
    _install(monkeypatch, facts=visible, all_facts=visible + [_absorbed("OBS23")])
    verdict, _ = pgc.check_plan("sotos")
    assert verdict.warnings == []

    _install(monkeypatch, facts=visible,
             all_facts=visible + [_absorbed("OBS23", state="rejected")])
    verdict, _ = pgc.check_plan("sotos")
    assert verdict.warnings == []
    # a rejected ticket with NO merge declaration is still warned about.
    _install(monkeypatch, facts=visible, all_facts=visible + [_ticket("OBS24", state="rejected")])
    verdict, _ = pgc.check_plan("sotos")
    assert [w.rule_id for w in verdict.warnings] == [pgc.R_REJECTED_WITHOUT_AUDIT]


def test_healthy_plan_emits_no_live_data_reasons_or_warnings(monkeypatch, capsys):
    """The acceptance bar: once the data is repaired the gate exits 0 and prints no warnings."""
    visible = _wellformed()
    _install(monkeypatch, facts=visible, all_facts=visible)
    assert pgc.main(["sotos"]) == 0
    assert capsys.readouterr().err == ""
