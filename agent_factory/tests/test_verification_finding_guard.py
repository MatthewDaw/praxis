"""A verification finding is not answered by changing nothing.

The post-merge verification round is the only thing that can see a defect living BETWEEN tickets --
two modules each individually green whose interfaces do not meet. It writes its judgement to
meta.regression_detail, but the completion gate reads only pinned checks, so the finding is prose
competing against "all your checks are green". Prose loses: a ticket was regressed with a report
naming the defect, its evidence and the required fix, and closed again TWICE with its file untouched,
because its tests hand-built the very shape the finding said was wrong.

The guard deliberately does NOT block completion on an open finding: verification runs only AFTER a
ticket finishes and merges, so that would deadlock -- the ticket could never reach the verification
that clears it. It fires only on the observed failure: finished, finding open, zero commits.

FL9/R16/E3: ``regression_detail`` is an ACCUMULATED LIST of findings (never a single dict a later
writer can clobber), and a legacy single-dict value read back is lifted into a one-entry list by the
shape guard -- so every assertion below exercises BOTH the legacy dict shape (still accepted on read)
and the accumulated list shape.
"""

import sys
from pathlib import Path

for _p in (str(Path(__file__).resolve().parents[1] / "src"),
           str(Path(__file__).resolve().parents[1] / "hooks")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _ticket_state as ts  # noqa: E402

FINDING = {"reason": "geometry and identity each define their own AcquisitionUnit",
           "evidence": "derive_flight_ids raises AttributeError on a geometry unit",
           "required_fix": "unify the type or carry a shared join key"}

FINDING_B = {"reason": "the ingestion CLI's --help crashes on an empty corpus",
             "evidence": "IndexError: list index out of range",
             "required_fix": "guard the empty-corpus case"}


def test_zero_commits_against_an_open_finding_is_refused():
    why = ts.finding_unanswered_without_change({"regression_detail": FINDING}, 0)
    assert why and "changed nothing" in why
    assert "AcquisitionUnit" in why, "the operator must see WHICH finding went unanswered"


def test_any_real_change_satisfies_it():
    """Non-negotiable: the guard must never be able to deadlock a ticket."""
    assert ts.finding_unanswered_without_change({"regression_detail": FINDING}, 1) is None


def test_a_resolved_finding_no_longer_gates():
    meta = {"regression_detail": dict(FINDING, resolved=True)}
    assert ts.finding_unanswered_without_change(meta, 0) is None
    assert ts.open_finding(meta) is None


def test_a_ticket_with_no_finding_is_untouched():
    for meta in ({}, {"regression_detail": None}, {"regression_detail": {}},
                 {"regression_detail": {"reason": "   "}}):
        assert ts.finding_unanswered_without_change(meta, 0) is None
        assert ts.open_finding(meta) is None


def test_resolve_finding_marks_it_answered():
    out = ts.resolve_finding({"regression_detail": FINDING})
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["resolved"] is True
    assert out[0]["reason"] == FINDING["reason"], "the report survives for the audit trail"


# --------------------------------------------------------------------------- FL9: accumulation (R16/E3)

def test_legacy_single_dict_reads_back_as_a_one_entry_list():
    """The read-side shape guard: a fact written before this ticket carries a bare dict, and every
    reader must treat it exactly like a one-entry accumulated list."""
    meta = {"regression_detail": FINDING}
    assert ts.regression_details(meta) == [FINDING]
    assert ts.open_findings(meta) == [FINDING]


def test_two_concurrent_findings_both_persist_and_both_stay_open():
    """accumulate_regression_detail must never clobber a sibling finding a concurrent writer just
    recorded -- two independent findings on the same ticket both persist and both inject on re-claim."""
    accumulated = ts.accumulate_regression_detail(FINDING, FINDING_B)
    assert len(accumulated) == 2
    reasons = {d["reason"] for d in accumulated}
    assert reasons == {FINDING["reason"], FINDING_B["reason"]}

    opens = ts.open_findings({"regression_detail": accumulated})
    assert len(opens) == 2, "no finding is clobbered by a later one"
    assert {d["reason"] for d in opens} == reasons


def test_accumulate_lifts_a_legacy_dict_before_appending():
    """The append path itself must run the SAME shape guard, not just the plain reader -- appending
    onto an existing legacy dict must not silently drop it."""
    accumulated = ts.accumulate_regression_detail(FINDING, FINDING_B)
    assert accumulated[0]["reason"] == FINDING["reason"]
    assert accumulated[1]["reason"] == FINDING_B["reason"]


def test_no_writer_site_emits_the_old_single_dict_shape():
    """A grep-level guarantee, executed: no producer in this codebase assigns a raw
    ``{"regression_detail": {...}}`` literal any more -- every one goes through
    ``accumulate_regression_detail`` (or ``resolve_finding``), which always returns a list."""
    import re
    root = Path(__file__).resolve().parents[1]
    pattern = re.compile(r'"regression_detail"\s*:\s*\{')
    offenders = []
    for path in (root / "scripts" / "af-ticket-loop.sh",
                 root / "src" / "agent_factory" / "ingestion_api.py"):
        text = path.read_text(encoding="utf-8")
        offenders.extend(f"{path}:{i+1}" for i, line in enumerate(text.splitlines())
                         if pattern.search(line))
    assert not offenders, f"raw single-dict regression_detail writes found: {offenders}"


def test_resolve_finding_resolves_every_open_finding_without_erasing_siblings():
    """Resolution must clear every currently-open finding but keep the full accumulated history --
    resolving one round's findings must never erase what a sibling just recorded."""
    accumulated = ts.accumulate_regression_detail(FINDING, FINDING_B)
    resolved = ts.resolve_finding({"regression_detail": accumulated}, resolved_by="verifier")
    assert len(resolved) == 2
    assert all(d["resolved"] for d in resolved)
    assert all(d["resolved_by"] == "verifier" for d in resolved)
    assert ts.open_findings({"regression_detail": resolved}) == []


def test_ticket_briefing_surfaces_every_open_finding():
    accumulated = ts.accumulate_regression_detail(FINDING, FINDING_B)
    text = ts.ticket_briefing("T1", {"regression_detail": accumulated})
    assert FINDING["reason"] in text
    assert FINDING_B["reason"] in text
    assert "finding 1/2" in text and "finding 2/2" in text


def test_ticket_briefing_injects_capped_provenance_marked_lessons():
    lessons = [{"id": f"lesson-{i}", "text": f"lesson text {i}"} for i in range(3)]
    text = ts.ticket_briefing("T1", {}, lessons=lessons)
    assert "UNTRUSTED DATA" in text
    for lesson in lessons:
        assert lesson["text"] in text
