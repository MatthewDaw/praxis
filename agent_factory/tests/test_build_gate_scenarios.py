"""Locks the build-completeness Stop gate's NEW behavior end-to-end, offline:

  * ARMING — inert for ordinary repo chat (no owned claim, no run marker); armed by a whole-set run
    marker OR a live owned claim,
  * WHOLE-SET enforcement — blocks while any scoped ready ticket remains (closing the between-ticket
    window), and reports ready vs waiting-on-deps separately,
  * DEPENDENCY STALL — armed work remains but nothing is ready (cycle / blocked-rooted chain) -> a
    clear, distinct block, not silent churn,
  * BLOCKED surfacing — terminal blocked tickets are excluded from churn but always surfaced,
  * SCOPE — another session's run marker leaves this session inert.

The only Praxis call the gate makes is ``incomplete_requirements``; we monkeypatch it, so this runs
deterministically with no network.
"""

import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import _praxis  # noqa: E402
import build_completeness_gate as gate  # noqa: E402

NOW = time.time()
OWNER = "sess-A"


def _run(monkeypatch, items, session=OWNER):
    monkeypatch.setattr(_praxis, "incomplete_requirements", lambda project, **k: items)
    monkeypatch.setenv("FACTORY_PROJECT", "prd-team-app")
    monkeypatch.delenv("FACTORY_GATE_DISABLED", raising=False)
    monkeypatch.setattr(sys, "stdin",
                        io.StringIO(json.dumps({"session_id": session, "cwd": "/x/team-app"})))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    with pytest.raises(SystemExit):
        gate.main()
    out = buf.getvalue().strip()
    parsed = json.loads(out) if out else {}
    # Normalize both hook output shapes: a block prints {"decision":"block","reason":...}; an allow
    # prints nothing, or {"hookSpecificOutput":{"additionalContext": advice}} when it carries advice.
    if parsed.get("decision") == "block":
        return {"decision": "block", "reason": parsed.get("reason", "")}
    advice = (parsed.get("hookSpecificOutput") or {}).get("additionalContext", "")
    return {"decision": "allow", "reason": advice}


def _marker(owner=OWNER):
    return {"run_owner": owner, "run_at": NOW, "run_scope": "all"}


def _claim(owner=OWNER):
    return {"build_state": "in_progress", "claim_owner": owner,
            "claim_heartbeat_at": NOW, "claim_lease_ttl": 900}


def _item(rid, **meta):
    m = {"requirement_id": rid}
    m.update(meta)
    return {"id": rid, "text": rid, "meta": m}


def test_inert_without_marker_or_claim(monkeypatch):
    # Ordinary repo conversation in a project that merely HAS open tickets: gate stays inert.
    assert _run(monkeypatch, [_item("R1")])["decision"] == "allow"


def test_run_marker_blocks_until_scope_done(monkeypatch):
    r = _run(monkeypatch, [_item("R1", **_marker())])
    assert r["decision"] == "block"
    assert "Ready to claim" in r["reason"]
    assert "ONE" in r["reason"]  # one-at-a-time instruction


def test_waiting_vs_ready_partition(monkeypatch):
    # R1 ready (marked, no deps); R2 marked but waits on R1.
    items = [_item("R1", **_marker()), _item("R2", depends_on=["R1"], **_marker())]
    r = _run(monkeypatch, items)
    assert r["decision"] == "block"
    assert "Ready to claim" in r["reason"]
    assert "Waiting on dependencies" in r["reason"] and "R2" in r["reason"]


def test_dependency_stall_is_distinct_block(monkeypatch):
    # A cycle inside the run: armed, work remains, nothing ready -> explicit stall, not silent churn.
    items = [_item("R1", depends_on=["R2"], **_marker()),
             _item("R2", depends_on=["R1"], **_marker())]
    r = _run(monkeypatch, items)
    assert r["decision"] == "block"
    assert "DEPENDENCY STALL" in r["reason"]


def test_blocked_surfaced_but_not_churned(monkeypatch):
    # Only a blocked ticket remains in the run -> allow (can't progress it) but surface it.
    items = [_item("R1", build_state="finished", **_marker()),
             _item("R2", build_state="blocked", block_reason="needs SMTP creds", **_marker())]
    r = _run(monkeypatch, items)
    assert r["decision"] == "allow"
    assert "R2" in r.get("reason", "") or "R2" in json.dumps(r)


def test_legacy_owned_claim_arms_gate(monkeypatch):
    # No run marker, but this session owns a live in_progress claim -> still enforced (fallback).
    r = _run(monkeypatch, [{**_item("R1"), "meta": _claim()}])
    assert r["decision"] == "block"


def test_other_sessions_run_leaves_me_inert(monkeypatch):
    # A run marker owned by a DIFFERENT session must not block this one.
    assert _run(monkeypatch, [_item("R1", **_marker("sess-B"))])["decision"] == "allow"


# --------------------------------------------------------------------------- project resolution


def _resolved_project(monkeypatch, cwd, session=OWNER):
    """Drive gate.main() and capture the ``project`` string it passes to ``incomplete_requirements``.

    Returns None if the gate never reached the Praxis read (e.g. it stood inert). Cleans up any
    ``FACTORY_PROJECT`` the gate loaded into the REAL environment from a ``.env`` so tests don't leak."""
    seen: dict[str, str] = {}

    def _spy(project, **_k):
        seen["project"] = project
        return []  # empty incomplete set -> gate allows; we only care about the project arg

    monkeypatch.setattr(_praxis, "incomplete_requirements", _spy)
    monkeypatch.delenv("FACTORY_GATE_DISABLED", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": session, "cwd": cwd})))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    try:
        with pytest.raises(SystemExit):
            gate.main()
    finally:
        # _load_dotenv writes straight into os.environ (not via monkeypatch), so drop it explicitly.
        os.environ.pop("FACTORY_PROJECT", None)
    return seen.get("project")


def test_factory_project_from_dotenv_wins_over_cwd_basename(monkeypatch, tmp_path):
    """REGRESSION: FACTORY_PROJECT set ONLY in a .env (not the real env) must be honored, and the
    cwd basename differs. The gate must load the factory .env BEFORE resolving the project, so it
    resolves ``prd-<FACTORY_PROJECT>`` — not ``prd-<cwd-basename>`` (the fail-open bug).

    A repo checked out as ``bestie-api`` building the ``google-shopping-scraper`` Praxis project."""
    monkeypatch.delenv("FACTORY_PROJECT", raising=False)  # absent from the REAL environment
    (tmp_path / ".env").write_text("FACTORY_PROJECT=google-shopping-scraper\n", encoding="utf-8")
    # _praxis._load_dotenv searches Path.cwd()/.env among its candidates — point it at our tmp .env.
    monkeypatch.setattr(_praxis.Path, "cwd", lambda: tmp_path)

    project = _resolved_project(monkeypatch, cwd="/repos/bestie-api")
    assert project == "prd-google-shopping-scraper"  # NOT prd-bestie-api


def test_real_env_factory_project_still_wins(monkeypatch, tmp_path):
    """A real shell ``FACTORY_PROJECT`` env var still wins over both the .env and the cwd basename
    (the .env load never overrides an already-set real env var)."""
    monkeypatch.setenv("FACTORY_PROJECT", "team-app")
    (tmp_path / ".env").write_text("FACTORY_PROJECT=other-project\n", encoding="utf-8")
    monkeypatch.setattr(_praxis.Path, "cwd", lambda: tmp_path)
    # _resolved_project pops FACTORY_PROJECT in teardown; restore the monkeypatched real one after.
    project = _resolved_project(monkeypatch, cwd="/repos/bestie-api")
    assert project == "prd-team-app"


def test_fail_closed_when_praxis_unreachable(monkeypatch):
    """REGRESSION guard: the early .env load must NOT turn the fail-closed path into a fail-open one.
    When the Praxis read raises PraxisUnreachable, the gate still BLOCKS loudly."""
    def _boom(project, **_k):
        raise _praxis.PraxisUnreachable("connection refused")

    monkeypatch.setattr(_praxis, "incomplete_requirements", _boom)
    monkeypatch.setenv("FACTORY_PROJECT", "team-app")
    monkeypatch.delenv("FACTORY_GATE_DISABLED", raising=False)
    monkeypatch.setattr(sys, "stdin",
                        io.StringIO(json.dumps({"session_id": OWNER, "cwd": "/x/bestie-api"})))
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    with pytest.raises(SystemExit):
        gate.main()
    parsed = json.loads(buf.getvalue().strip())
    assert parsed.get("decision") == "block"
    assert "PRAXIS UNREACHABLE" in parsed.get("reason", "")
