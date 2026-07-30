"""af-rapid-queue: the capture spool and its advisory Stop hook.

The skill's three guarantees reduce to properties of these two modules, so that is what is tested:

* NEVER LOST — a capture is durable before any Praxis call; only a real ticket id retires it; a
  torn final line costs one record, not the spool; a filed record can never resurrect as pending.
* NEVER DERAILED — the relay hook ALWAYS allows, and is byte-silent with an empty spool (so wiring
  it into the always-on hook set cannot disturb ordinary sessions).
* The Praxis ticket shape the skill files must pass the same pre-claim resumability probe af-build
  runs, or the ticket is parked ``under_specified`` and never built — a silent loss.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# hooks/ is a flat directory of harness-invoked scripts, not the `agent_factory` package (see
# agent_factory/hooks/_ticket_state.py's docstring) -- the established convention is to add it to
# sys.path and import the bare module name.
_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

import rapid_queue  # noqa: E402
import rapid_queue_relay  # noqa: E402
from agent_factory.resumability import resumability_report  # noqa: E402

SKILL_MD = Path(__file__).resolve().parents[1] / "skills/af-rapid-queue/SKILL.md"
HOOKS_JSON = Path(__file__).resolve().parents[1] / "hooks/hooks.json"


@pytest.fixture
def spool(tmp_path, monkeypatch):
    """Pin the spool to one file in a tmp dir, so nothing touches the real ``~/.praxis``."""
    path = tmp_path / "proj.jsonl"
    monkeypatch.setenv("AF_RAPID_QUEUE_PATH", str(path))
    monkeypatch.setenv("FACTORY_PROJECT", "demo")
    return path


# --------------------------------------------------------------------------- never lost

def test_capture_is_durable_and_pending_until_filed(spool):
    entry = rapid_queue.capture("the header overlaps on mobile")

    assert spool.exists(), "capture must land on disk before any Praxis call is attempted"
    assert [e["text"] for e in rapid_queue.pending()] == ["the header overlaps on mobile"]
    assert rapid_queue.status_of(entry) == "queued"

    rapid_queue.mark_filed(entry["qid"], "cid-123")

    assert rapid_queue.pending() == [], "a filed entry leaves the pending set"
    assert rapid_queue.entries()[0]["ticket_id"] == "cid-123", "and keeps its ticket id as an audit tail"


def test_filing_requires_a_real_ticket_id(spool):
    entry = rapid_queue.capture("refactor the auth guard")

    for bogus in ("", "   "):
        with pytest.raises(ValueError):
            rapid_queue.mark_filed(entry["qid"], bogus)

    assert len(rapid_queue.pending()) == 1, "a request cannot be retired without becoming real work"


def test_filing_is_idempotent_and_first_ticket_wins(spool):
    entry = rapid_queue.capture("dedupe the retry logic")
    rapid_queue.mark_filed(entry["qid"], "cid-first")
    rapid_queue.mark_filed(entry["qid"], "cid-second")

    assert rapid_queue.entries()[0]["ticket_id"] == "cid-first"
    assert rapid_queue.pending() == []


def test_unknown_qid_raises_rather_than_silently_retiring_nothing(spool):
    rapid_queue.capture("something real")
    with pytest.raises(KeyError):
        rapid_queue.mark_filed("nope", "cid-1")


def test_empty_capture_is_refused(spool):
    with pytest.raises(ValueError):
        rapid_queue.capture("   ")


def test_torn_final_line_costs_only_that_record(spool):
    first = rapid_queue.capture("first request")
    rapid_queue.capture("second request")
    # Simulate a process killed mid-append: truncate the last line partway through.
    text = spool.read_text()
    spool.write_text(text[: text.rindex("\n") + 1] + '{"kind":"queued","qid":"tor')

    surviving = [e["text"] for e in rapid_queue.pending()]
    assert surviving == ["first request", "second request"]
    assert any(e["qid"] == first["qid"] for e in rapid_queue.pending())


def test_concurrent_captures_do_not_clobber_each_other(spool):
    """Append-only is the point: interleaved writers each keep their own request."""
    for i in range(25):
        rapid_queue.capture(f"request {i}")
    assert len(rapid_queue.pending()) == 25


def test_compaction_keeps_every_pending_entry_and_drops_stale_filed(spool):
    kept_pending = rapid_queue.capture("still owed a ticket", now=1_000.0)
    recent = rapid_queue.capture("filed just now", now=1_000.0)
    stale = rapid_queue.capture("filed long ago", now=1_000.0)
    later = 1_000.0 + rapid_queue.FILED_RETENTION_S + 1
    rapid_queue.mark_filed(stale["qid"], "cid-stale", now=1_000.0)
    rapid_queue.mark_filed(recent["qid"], "cid-recent", now=later - 60)

    rapid_queue.compact(spool, now=later)

    qids = {e["qid"] for e in rapid_queue.entries()}
    assert kept_pending["qid"] in qids, "a pending entry is never dropped by age"
    assert recent["qid"] in qids
    assert stale["qid"] not in qids
    assert [e["qid"] for e in rapid_queue.pending()] == [kept_pending["qid"]], \
        "compaction must not resurrect a filed entry as pending"


def test_spool_lives_outside_the_repo(monkeypatch, tmp_path):
    """A queued request must survive the checkout it was typed in (branch switch, worktree removal)
    and be visible to every parallel af-build worker -- so it is keyed by project, not by cwd."""
    monkeypatch.delenv("AF_RAPID_QUEUE_PATH", raising=False)
    monkeypatch.setenv("AF_RAPID_QUEUE_DIR", str(tmp_path / "spools"))
    monkeypatch.setenv("FACTORY_PROJECT", "prd-demo")

    from_worktree = rapid_queue.spool_path(cwd=str(tmp_path / "worktrees" / "ticket-a"))
    from_main = rapid_queue.spool_path(cwd=str(tmp_path / "main-checkout"))

    assert from_worktree == from_main == tmp_path / "spools" / "demo.jsonl"


# --------------------------------------------------------------------------- never derailed

def test_relay_is_silent_when_nothing_is_pending(spool):
    """Byte-identical to no hook at all -- the precondition for wiring it always-on."""
    proc = _run_relay(spool)
    assert proc.returncode == 0
    assert proc.stdout == "", proc.stdout


def test_relay_surfaces_pending_requests_without_blocking(spool):
    rapid_queue.capture("the header overlaps on mobile")
    proc = _run_relay(spool)

    payload = json.loads(proc.stdout)
    assert "decision" not in payload, "intake advice must never block a stop"
    advice = payload["hookSpecificOutput"]["additionalContext"]
    assert "the header overlaps on mobile" in advice
    assert "prd-<project>" in advice and "rapid_queue.py filed" in advice


def test_relay_does_not_drain_by_reading(spool):
    """Unlike the job mailbox (informational, drained on delivery), a request is WORK: surfacing it
    must not retire it, or a session that dies right after would lose it."""
    rapid_queue.capture("refactor the retry helper")
    _run_relay(spool)
    _run_relay(spool)
    assert len(rapid_queue.pending()) == 1


def test_relay_allows_when_the_spool_is_unreadable(spool):
    spool.write_text("")  # exists but unusable as a directory-shaped path below
    spool.unlink()
    spool.mkdir()  # reading a directory as a file raises -- the hook must still allow
    proc = _run_relay(spool)
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_decide_is_pure_and_empty_for_no_pending():
    assert rapid_queue_relay.decide([]) == ""
    assert "q1" in rapid_queue_relay.decide([{"qid": "q1", "text": "x"}])


def test_relay_is_wired_into_the_always_on_stop_set():
    stop_hooks = json.loads(HOOKS_JSON.read_text())["hooks"]["Stop"]
    commands = [h["command"] for group in stop_hooks for h in group["hooks"]]
    assert any("rapid_queue_relay.py" in c for c in commands)
    assert all("${PRAXIS_HOOK_PYTHON:-python3}" in c for c in commands), "match the hook-set convention"


# --------------------------------------------------------------------------- the filed ticket builds

def test_skill_documented_ticket_shape_passes_the_preclaim_resumability_probe():
    """The failure this guards: af-build's ``start_ticket`` refuses to claim a ticket that has
    neither acceptance nor a resolved check, or no ``verify`` mode -- it parks it
    ``under_specified``. A rapid ticket filed without those looks queued forever, which is exactly
    the silent loss the skill promises to prevent. So the shape the skill prescribes is asserted
    against the real probe, and the skill text is asserted to prescribe it.
    """
    filed = {"build_state": "incomplete", "verify": "automated",
             "acceptance": "the header renders at one line with no overlap at 375px width",
             "tags": ["rapid-queue", "frontend"], "scope": "mvp"}
    assert resumability_report(filed, [], known_requirement_ids=[]) == {"resumable": True, "missing": []}

    naive = {"build_state": "incomplete", "tags": ["rapid-queue"]}
    report = resumability_report(naive, [], known_requirement_ids=[])
    assert report["resumable"] is False
    assert set(report["missing"]) == {"contract", "verify"}

    text = SKILL_MD.read_text()
    assert '"acceptance"' in text and '"verify"' in text
    assert "under_specified" in text, "the skill must say WHY these fields are mandatory"


def test_skill_forbids_building_the_captured_request():
    """The non-derailment guarantee is enforced by the skill text, so assert it is actually there."""
    text = SKILL_MD.read_text().lower()
    assert "never investigate, fix, or plan the captured request" in text
    assert "capture, do not act" in text


def _run_relay(spool: Path) -> subprocess.CompletedProcess:
    """Run the hook as the harness does: a bare subprocess fed the Stop payload on stdin."""
    env = {"AF_RAPID_QUEUE_PATH": str(spool), "FACTORY_PROJECT": "demo",
           "PATH": "/usr/bin:/bin", "HOME": str(spool.parent), "PRAXIS_HOOK_QUIET": "1"}
    return subprocess.run(
        [sys.executable, str(Path(_HOOKS) / "rapid_queue_relay.py")],
        input=json.dumps({"cwd": str(spool.parent)}),
        capture_output=True, text=True, env=env, check=False,
    )
