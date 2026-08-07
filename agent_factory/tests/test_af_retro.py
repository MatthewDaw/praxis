"""FL18 — the full ingestion/lifecycle record (R23) plus push-not-pull flags (R24).

Covers the ticket's acceptance condition end-to-end: after a run containing a suspension,
``af-retro <project>`` shows it, the loop-end notification names it (``af-retro --flags <project>``),
the unacknowledged flag persists until acked via ``af-retro ack``, ``af-retro --flags`` aggregates
pending flags across every project newest first, and acking a flag removes it from the pending
list and records who/when.
"""

from __future__ import annotations

import time
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


def test_cli_report_requires_project_without_flags() -> None:
    with pytest.raises(SystemExit):
        af_retro.main([])


def test_cli_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        af_retro.main(["--help"])
    assert exc.value.code == 0
