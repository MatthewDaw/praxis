"""FL18 — the full ingestion/lifecycle record (R23) plus push-not-pull flags (R24).

Covers the ticket's acceptance condition end-to-end: after a run containing a suspension,
``af-retro <project>`` shows it, the loop-end notification names it (``af-retro --flags <project>``),
the unacknowledged flag persists until acked via ``af-retro ack``, ``af-retro --flags`` aggregates
pending flags across every project newest first, and acking a flag removes it from the pending
list and records who/when.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import pytest
from hooks import _praxis

from agent_factory import af_retro, ingestion_api


class _WhoAmIStub:
    def __init__(self, ok: bool, principal: str = "unit-tester") -> None:
        self.ok = ok
        self.principal = principal
        self.detail = ""


class _FakeStore:
    """A minimal in-memory double for the shared factory-learnings space: enough of
    ``_praxis``'s surface (``_request`` POST /insights, ``facts_by``, ``patch_meta``) for
    ``emit_flag``/``read_flags``/``ack_flag``/``suspend``/``read_checks`` to round-trip."""

    def __init__(self) -> None:
        self.facts: dict[str, dict[str, Any]] = {}
        self._n = 0

    def _new_id(self) -> str:
        self._n += 1
        return f"fake-{self._n}"

    def request(self, method: str, path: str, *, body: dict[str, Any] | None = None,
               params: dict[str, Any] | None = None, space: str | None = None,
               snapshot: str | None = None, **kw: Any) -> dict[str, Any]:
        if method == "POST" and path == "/insights":
            fid = self._new_id()
            fact = {"id": fid, "text": (body or {}).get("insight", ""),
                    "category": (body or {}).get("category"),
                    "meta": dict((body or {}).get("meta") or {})}
            self.facts[fid] = fact
            return {"id": fid, "action": "added"}
        return {}

    def facts_by(self, category: str | None = None, meta: dict[str, Any] | None = None,
                state: str = "active", space: str | None = None,
                snapshot: str | None = None) -> list[dict[str, Any]]:
        out = []
        for fact in self.facts.values():
            if category is not None and fact["category"] != category:
                continue
            if meta:
                ok = True
                for k, v in meta.items():
                    fv = fact["meta"].get(k)
                    if isinstance(fv, list):
                        ok = ok and v in fv
                    else:
                        ok = ok and fv == v
                if not ok:
                    continue
            out.append(fact)
        return out

    def patch_meta(self, cid: str, meta_dict: dict[str, Any], *, space: str | None = None,
                   snapshot: str | None = None) -> dict[str, Any]:
        self.facts[cid]["meta"].update(meta_dict)
        return self.facts[cid]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    st = _FakeStore()
    monkeypatch.setattr(_praxis, "whoami", lambda: _WhoAmIStub(True, "unit-tester"))
    monkeypatch.setattr(_praxis, "ensure_space", lambda *a, **kw: a[0])
    monkeypatch.setattr(_praxis, "_request", st.request)
    monkeypatch.setattr(_praxis, "facts_by", st.facts_by)
    monkeypatch.setattr(_praxis, "patch_meta", st.patch_meta)
    return st


# --------------------------------------------------------------------------- ingestion_api primitives


def test_emit_flag_rejects_unknown_kind(store: _FakeStore) -> None:
    with pytest.raises(ValueError):
        ingestion_api.emit_flag("not-a-real-kind", "proj-a", {"reason": "x"})


def test_emit_flag_writes_a_pending_flag_into_the_flags_snapshot(
    store: _FakeStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    orig = store.request

    def recording(method: str, path: str, *, body: dict[str, Any] | None = None,
                 **kw: Any) -> dict[str, Any]:
        calls.append({"method": method, "path": path, "body": body, **kw})
        return orig(method, path, body=body, **kw)

    monkeypatch.setattr(_praxis, "_request", recording)
    ingestion_api.emit_flag("suspension", "proj-a", {"reason": "flaky"})
    insight_calls = [c for c in calls if c["path"] == "/insights"]
    assert len(insight_calls) == 1
    assert insight_calls[0]["snapshot"] == _praxis.FACTORY_FLAGS_SNAPSHOT
    assert insight_calls[0]["space"] == _praxis.FACTORY_LEARNINGS_SPACE


def test_suspend_emits_a_suspension_flag(store: _FakeStore) -> None:
    store.facts["c1"] = {"id": "c1", "category": "check",
                         "meta": {"check_id": "c1", "enforcement_state": "gating"}}
    ingestion_api.suspend("c1", "proj-a", "3 consecutive false-positive regressions")
    flags = ingestion_api.read_flags("proj-a")
    assert len(flags) == 1
    assert flags[0]["meta"]["kind"] == "suspension"
    assert flags[0]["meta"]["reason"] == "3 consecutive false-positive regressions"
    assert flags[0]["meta"]["acknowledged"] is False


def test_kill_switch_also_emits_a_suspension_flag(store: _FakeStore) -> None:
    store.facts["c2"] = {"id": "c2", "category": "check",
                         "meta": {"check_id": "c2", "enforcement_state": "gating"}}
    ingestion_api.kill_switch("c2", "proj-a", "operator override")
    flags = ingestion_api.read_flags("proj-a")
    assert len(flags) == 1
    assert flags[0]["meta"]["kill_switch"] is True


def test_read_flags_pending_only_excludes_acked(store: _FakeStore) -> None:
    written = ingestion_api.emit_flag("parking", "proj-a", {"reason": "regress-cycle cap"})
    ingestion_api.ack_flag(written["id"])
    assert ingestion_api.read_flags("proj-a") == []
    all_flags = ingestion_api.read_flags("proj-a", pending_only=False)
    assert len(all_flags) == 1
    assert all_flags[0]["meta"]["acknowledged"] is True


def test_ack_flag_records_who_and_when(store: _FakeStore) -> None:
    written = ingestion_api.emit_flag("undraftable", "proj-a", {"reason": "budget exhausted"})
    result = ingestion_api.ack_flag(written["id"])
    meta = result["meta"]
    assert meta["acknowledged"] is True
    assert meta["acknowledged_by"] == "unit-tester"
    assert isinstance(meta["acknowledged_at"], float)


def test_read_flags_aggregates_across_projects_newest_first(store: _FakeStore) -> None:
    ingestion_api.emit_flag("suspension", "proj-a", {"reason": "older"})
    time.sleep(0.01)
    ingestion_api.emit_flag("check-defeat", "proj-b", {"reason": "newer"})
    flags = ingestion_api.read_flags()  # no project -> every project
    assert [f["meta"]["project"] for f in flags] == ["proj-b", "proj-a"]


def test_read_checks_reads_any_lifecycle_state(store: _FakeStore) -> None:
    store.facts["c3"] = {"id": "c3", "category": "check",
                         "meta": {"check_id": "c3", "enforcement_state": "suspended"}}
    checks = ingestion_api.read_checks("proj-a")
    assert [c["id"] for c in checks] == ["c3"]


# --------------------------------------------------------------------------- af-retro pure stats helpers


def test_gating_vs_demoted_and_undraftable_rate() -> None:
    checks = [
        {"meta": {"enforcement_state": "gating", "channel": "human"}},
        {"meta": {"enforcement_state": "report_only", "channel": "machine", "proof_status": "unproven"}},
        {"meta": {"enforcement_state": "suspended", "channel": "machine", "proof_status": "proven"}},
    ]
    assert af_retro.gating_vs_demoted(af_retro.enforcement_counts(checks)) == (1, 2)
    assert af_retro.check_undraftable_rate(checks) == pytest.approx(0.5)


def test_a_check_with_no_recorded_enforcement_state_is_not_counted_as_demoted() -> None:
    """D3 — `unknown` means the fact predates FL12's state machine, NOT that something demoted it.
    Counting it as demoted inflated the enforcement-decay ratio in the alarming direction."""
    checks = [
        {"meta": {"enforcement_state": "gating"}},
        {"meta": {}},                                   # no enforcement_state recorded at all
        {"meta": {"enforcement_state": "archived"}},
    ]
    counts = af_retro.enforcement_counts(checks)
    assert counts[af_retro.UNKNOWN_STATE] == 1
    assert af_retro.gating_vs_demoted(counts) == (1, 1)
    assert af_retro.unclassified_count(counts) == 1


def test_every_stateless_check_reports_a_zero_demoted_ratio_not_a_total_wipeout() -> None:
    """The worst case the old classification produced: a project whose checks all predate the
    state machine reported 0:N — "enforcement has entirely decayed" — when nothing was demoted."""
    counts = af_retro.enforcement_counts([{"meta": {}} for _ in range(5)])
    assert af_retro.gating_vs_demoted(counts) == (0, 0)
    assert af_retro.unclassified_count(counts) == 5


# --------------------------------------------------------------------------- R23 run record (D4)


def test_parse_since_accepts_durations_epochs_and_iso() -> None:
    now = time.time()
    assert af_retro.parse_since("24h") == pytest.approx(now - 86400, abs=5)
    assert af_retro.parse_since("7d") == pytest.approx(now - 7 * 86400, abs=5)
    assert af_retro.parse_since("90m") == pytest.approx(now - 5400, abs=5)
    assert af_retro.parse_since("1700000000") == 1700000000.0
    assert af_retro.parse_since("2024-01-02T03:04:05") == datetime(2024, 1, 2, 3, 4, 5).timestamp()
    with pytest.raises(ValueError):
        af_retro.parse_since("last tuesday")


def test_run_record_counts_only_events_inside_the_window() -> None:
    """The per-run record scopes by the timestamp the transition itself wrote — an older event of
    the same kind is outside the window and must not be counted into this run."""
    now = time.time()
    old, new = now - 10 * 86400, now - 60
    checks = [
        {"meta": {"enforcement_state": "gating", "promoted_at": new, "proof_status": "proven",
                  "createdAt": new}},
        {"meta": {"enforcement_state": "gating", "promoted_at": old, "proof_status": "proven",
                  "createdAt": old}},
        {"meta": {"enforcement_state": "suspended", "suspended_at": new, "createdAt": new,
                  "proof_status": "unproven"}},
        {"meta": {"enforcement_state": "report_only", "check_defeat_at": new, "createdAt": old,
                  "proof_status": "unproven"}},
        {"meta": {"enforcement_state": "gating", "widened_at": new, "createdAt": old}},
    ]
    flags = [{"meta": {"at": new}}, {"meta": {"at": old}}]
    lessons = [{"meta": {"createdAt": new}}, {"meta": {"createdAt": old}}]

    record = af_retro.run_record(checks, flags, lessons, since=now - 86400)
    assert record["events"] == {"activated": 1, "suspended": 1, "widened": 1, "demoted": 1,
                                "archived": 0}
    assert record["flags_raised"] == 1
    assert record["lessons_ingested"] == 1
    assert dict(record["proof_outcomes"]) == {"proven": 1, "unproven": 1}

    # the same corpus over all history counts the older events too
    everything = af_retro.run_record(checks, flags, lessons, since=None)
    assert everything["events"]["activated"] == 2
    assert everything["flags_raised"] == 2
    assert everything["lessons_ingested"] == 2


def test_run_record_reports_undated_events_instead_of_assuming_a_window() -> None:
    """A check that IS suspended but records no `suspended_at` cannot be placed in or out of the
    window; it is reported as undated rather than counted either way."""
    now = time.time()
    checks = [{"meta": {"enforcement_state": "suspended"}},
              {"meta": {"enforcement_state": "gating"}}]
    record = af_retro.run_record(checks, [], [{"meta": {}}], since=now - 86400)
    assert record["events"]["suspended"] == 0
    assert record["undated"]["suspended"] == 1
    assert record["undated"]["activated"] == 1
    assert record["lessons_undated"] == 1
    assert record["proof_outcomes_undated"] == 2
    # the dimensions this corpus genuinely cannot answer are named, never faked as zero
    assert any("regressions" in gap for gap in record["gaps"])
    assert any("run identity" in gap for gap in record["gaps"])


def test_check_undraftable_rate_is_zero_with_no_machine_checks() -> None:
    assert af_retro.check_undraftable_rate([{"meta": {"channel": "human"}}]) == 0.0


# --------------------------------------------------------------------------- CLI / acceptance


def test_acceptance_suspension_flag_lifecycle_end_to_end(
    store: _FakeStore, capsys: pytest.CaptureFixture[str],
) -> None:
    """The ticket's acceptance condition, in one pass: a suspension is raised on proj-a; the
    project report shows it; the loop-end command (`af-retro --flags <project>`) names it; it
    persists pending across every later read (the af-build session-start surfacing) until acked
    via `af-retro ack`; `af-retro --flags` aggregates two projects' pending flags newest first;
    acking drops it from the pending list and records who/when."""
    store.facts["c1"] = {"id": "c1", "category": "check",
                         "meta": {"check_id": "c1", "enforcement_state": "gating"}}
    ingestion_api.suspend("c1", "proj-a", "3 consecutive false-positive regressions")
    time.sleep(0.01)
    ingestion_api.emit_flag("check-defeat", "proj-b", {"reason": "symptom still present"})

    # af-retro <project> shows the suspension in its own project report.
    rc = af_retro.main(["proj-a"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "suspension" in out
    assert "3 consecutive false-positive regressions" in out
    assert "PENDING" in out

    # The loop-end notification names it (this is the exact command af-ticket-loop.sh runs).
    rc = af_retro.main(["--flags", "proj-a"])
    assert rc == 0
    loop_end_out = capsys.readouterr().out
    assert "suspension" in loop_end_out
    assert "PENDING" in loop_end_out

    # --flags with no project aggregates every project, newest first — the af-build
    # session-start surfacing reads from the same pending list.
    rc = af_retro.main(["--flags"])
    assert rc == 0
    agg_out = capsys.readouterr().out
    lines = [ln for ln in agg_out.splitlines() if ln.strip().startswith("[")]
    assert len(lines) == 2
    assert "proj-b" in lines[0] and "check-defeat" in lines[0]  # newest first
    assert "proj-a" in lines[1] and "suspension" in lines[1]

    # Ack the proj-a suspension.
    pending = ingestion_api.read_flags("proj-a")
    assert len(pending) == 1
    flag_id = pending[0]["id"]
    rc = af_retro.main(["ack", flag_id])
    assert rc == 0
    ack_out = capsys.readouterr().out
    assert flag_id in ack_out
    assert "unit-tester" in ack_out

    # Acking removed it from the pending list; who/when is recorded.
    assert ingestion_api.read_flags("proj-a") == []
    acked = ingestion_api.read_flags("proj-a", pending_only=False)[0]
    assert acked["meta"]["acknowledged"] is True
    assert acked["meta"]["acknowledged_by"] == "unit-tester"
    assert acked["meta"]["acknowledged_at"] is not None

    # The aggregate now only shows proj-b's pending check-defeat flag.
    rc = af_retro.main(["--flags"])
    assert rc == 0
    final_out = capsys.readouterr().out
    assert "proj-b" in final_out and "check-defeat" in final_out
    assert "proj-a" not in final_out


def test_cli_report_prints_the_run_record_scoped_by_since(
    store: _FakeStore, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through `af_retro.main` — the entry point `python -m agent_factory.af_retro` runs — a
    project report carries the R23 run record, and `--since` scopes it."""
    monkeypatch.setattr(_praxis, "context", lambda *a, **kw: [])
    now = time.time()
    store.facts["c1"] = {"id": "c1", "category": "check", "meta": {
        "check_id": "c1", "enforcement_state": "suspended", "suspended_at": now - 30,
        "createdAt": now - 30, "proof_status": "proven"}}
    store.facts["c2"] = {"id": "c2", "category": "check", "meta": {
        "check_id": "c2", "enforcement_state": "suspended", "suspended_at": now - 30 * 86400,
        "createdAt": now - 30 * 86400, "proof_status": "proven"}}

    def record_line(out: str) -> str:
        return next(ln for ln in out.splitlines() if ln.startswith("af-retro: run record ("))

    assert af_retro.main(["proj-a", "--since", "24h"]) == 0
    scoped = capsys.readouterr().out
    assert "suspended=1" in record_line(scoped)   # only the recent suspension is in the window
    assert "since " in record_line(scoped)
    assert "run record GAP" in scoped             # unanswerable dimensions stated, never faked

    assert af_retro.main(["proj-a"]) == 0
    whole = capsys.readouterr().out
    assert "suspended=2" in record_line(whole)    # whole history
    assert "all history" in record_line(whole)


def test_cli_rejects_an_unparseable_since() -> None:
    with pytest.raises(SystemExit):
        af_retro.main(["proj-a", "--since", "whenever"])


def test_cli_report_requires_project_without_flags() -> None:
    with pytest.raises(SystemExit):
        af_retro.main([])


def test_cli_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        af_retro.main(["--help"])
    assert exc.value.code == 0
