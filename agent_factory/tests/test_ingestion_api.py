"""FL1 — the shared factory-learnings space, its sole writer, and the no-file-canonical guarantee.

Covers the ticket's acceptance condition: a lesson written to the ``factory-learnings`` space is
readable read-only from any project session, nothing but ``agent_factory.ingestion_api`` writes
into that space, and no reader anywhere treats a local file as the canonical lesson store.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from agent_factory import ingestion_api
from hooks import _praxis

REPO_ROOT = Path(__file__).resolve().parents[2]  # .../agent_factory/tests -> repo root
AGENT_FACTORY_SRC = REPO_ROOT / "agent_factory" / "src"
AGENT_FACTORY_HOOKS = REPO_ROOT / "agent_factory" / "hooks"


# --------------------------------------------------------------------------- write path (mocked)

def test_write_lesson_targets_the_factory_learnings_snapshot(monkeypatch):
    """``write_lesson`` is a POST /insights bound to (factory-learnings, lessons) — never working
    memory, never any other space."""
    calls = []

    def fake_request(method, path, *, body=None, space=None, snapshot=None, **kw):
        calls.append({"method": method, "path": path, "body": body,
                      "space": space, "snapshot": snapshot})
        return {"id": "fake-id", "action": "added"}

    monkeypatch.setattr(_praxis, "_request", fake_request)
    monkeypatch.setattr(_praxis, "ensure_space", lambda *a, **kw: a[0])

    ingestion_api.write_lesson("always run the migration before the smoke test", source="unit-test")

    assert len(calls) == 1
    call = calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/insights"
    assert call["space"] == _praxis.FACTORY_LEARNINGS_SPACE == "factory-learnings"
    assert call["snapshot"] == _praxis.FACTORY_LEARNINGS_SNAPSHOT == "lessons"
    assert call["body"]["category"] == "lesson"
    assert call["body"]["insight"] == "always run the migration before the smoke test"


def test_write_lesson_rejects_empty_text():
    with pytest.raises(ValueError):
        ingestion_api.write_lesson("   ")


def test_write_lesson_bootstraps_the_space(monkeypatch):
    """A snapshot-bound write into a never-created space 404s server-side, so the sole writer must
    idempotently ensure its space exists before writing."""
    ensured = []
    monkeypatch.setattr(_praxis, "ensure_space", lambda sid, name=None: ensured.append(sid) or sid)
    monkeypatch.setattr(_praxis, "_request", lambda *a, **kw: {"id": "x"})

    ingestion_api.write_lesson("some lesson")

    assert ensured == [_praxis.FACTORY_LEARNINGS_SPACE]


# --------------------------------------------------------------------------- read path (mocked, GET-only)

def test_read_lessons_is_scoped_to_the_shared_space_and_never_writes(monkeypatch):
    monkeypatch.setattr(_praxis, "context",
                        lambda q, top_k=10, space=None, snapshot=None: [{"id": "1", "text": q}])
    hits = ingestion_api.read_lessons("flaky retries", top_k=3)
    assert hits == [{"id": "1", "text": "flaky retries"}]


def test_read_lessons_empty_query_enumerates_by_category(monkeypatch):
    captured = {}

    def fake_facts_by(category=None, meta=None, state="active", space=None, snapshot=None):
        captured.update(category=category, space=space, snapshot=snapshot)
        return [{"id": "2", "text": "lesson text"}]

    monkeypatch.setattr(_praxis, "facts_by", fake_facts_by)
    hits = ingestion_api.read_lessons()
    assert hits == [{"id": "2", "text": "lesson text"}]
    assert captured == {
        "category": "lesson",
        "space": _praxis.FACTORY_LEARNINGS_SPACE,
        "snapshot": _praxis.FACTORY_LEARNINGS_SNAPSHOT,
    }


# --------------------------------------------------------------------------- dedup (exact, not semantic top-k)

def test_classify_and_dedup_catches_exact_twin_crowded_out_of_semantic_topk(monkeypatch):
    """Regression: the exact-text duplicate must be found by an EXACT content-hash lookup against
    the whole corpus, NOT by the semantic top-k recall that could push an exact twin out of the
    ranking whenever enough nearer neighbours exist (the bug that let bulk ingests duplicate rows)."""
    text = "always run the migration before the smoke test"
    content_hash = ingestion_api._hash_text(text.strip().lower())
    existing = {"id": "lesson-existing", "category": "lesson",
                "meta": {"content_hash": content_hash}, "text": text}

    def fake_facts_by(category=None, meta=None, state="active", space=None, snapshot=None):
        if meta and meta.get("content_hash") == content_hash:
            return [existing]
        return [existing]  # exhaustive enumeration also carries it

    # A semantic lane that DELIBERATELY does not surface the exact twin — the old top-k path.
    monkeypatch.setattr(_praxis, "context", lambda *a, **kw: [{"id": "n", "text": "something else"}])
    monkeypatch.setattr(_praxis, "facts_by", fake_facts_by)

    result = ingestion_api.classify_and_dedup(text)
    assert result["duplicate_of"] == "lesson-existing"
    assert result["content_hash"] == content_hash


def test_classify_and_dedup_reports_no_duplicate_for_a_novel_lesson(monkeypatch):
    monkeypatch.setattr(_praxis, "context", lambda *a, **kw: [])
    monkeypatch.setattr(_praxis, "facts_by", lambda *a, **kw: [])
    result = ingestion_api.classify_and_dedup("a brand new never-before-seen complaint")
    assert result["duplicate_of"] is None
    assert result["content_hash"]


# --------------------------------------------------------------------------- CLI

def test_cli_ingest_calls_write_lesson(monkeypatch):
    captured = {}
    monkeypatch.setattr(ingestion_api, "write_lesson",
                        lambda text, source=None, meta=None: captured.update(
                            text=text, source=source) or {"summary": "ok"})
    rc = ingestion_api.main(["ingest", "a real lesson", "--source", "merger"])
    assert rc == 0
    assert captured == {"text": "a real lesson", "source": "merger"}


def test_cli_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        ingestion_api.main(["--help"])
    assert exc.value.code == 0


# --------------------------------------------------------------------------- live round trip

def _live_praxis_opted_in() -> bool:
    """Whether this run may talk to the REAL Praxis backend.

    Reachability alone used to decide, which made the default unit suite non-reproducible by
    construction: the identical command ran a live network test that wrote into (and deleted from)
    the shared cloud ``factory-learnings`` space whenever the service happened to be up, and
    reported an extra skip whenever it happened to be down — same command, different results, and
    a moving skipped count with it. Opting in explicitly makes the default suite deterministic
    without deleting the coverage: ``AF_LIVE_PRAXIS_TESTS=1 uv run pytest tests/test_ingestion_api.py``
    still runs the round trip (and still skips, loudly, if the backend is genuinely down).
    """
    if os.environ.get("AF_LIVE_PRAXIS_TESTS") != "1":
        return False
    try:
        _praxis.ping()
        return True
    except _praxis.PraxisUnreachable:
        return False


@pytest.mark.skipif(not _live_praxis_opted_in(),
                    reason="live Praxis round trip is opt-in: set AF_LIVE_PRAXIS_TESTS=1")
def test_live_write_then_read_then_mount_round_trip():
    """The end-to-end acceptance behavior: a lesson written via the ingestion API is readable
    (read-only) from a working-memory session that mounts the shared space, with no local file
    ever involved."""
    marker = f"fl1-live-test-{id(object())}"
    text = f"{marker}: a lesson only the ingestion API could have written"
    written = ingestion_api.write_lesson(text, source="test_ingestion_api")
    fact_id = written.get("id")
    try:
        # Direct scoped read (facts_by against the shared snapshot) sees it.
        hits = ingestion_api.read_lessons()
        assert any(marker in (h.get("text") or "") for h in hits)

        # Mounting the shared space onto working memory surfaces it in an ordinary context()
        # call that names no space/snapshot at all — the "read from within any project space"
        # guarantee. Mounting an already-populated snapshot never 404s.
        _praxis.mount_snapshot(_praxis.FACTORY_LEARNINGS_SPACE, _praxis.FACTORY_LEARNINGS_SNAPSHOT)
        mounted_hits = _praxis.context(marker, top_k=5)
        assert any(h.get("id") == fact_id and h.get("mounted") for h in mounted_hits)
    finally:
        if fact_id:
            _praxis.delete_fact(fact_id, space=_praxis.FACTORY_LEARNINGS_SPACE,
                                snapshot=_praxis.FACTORY_LEARNINGS_SNAPSHOT)


# --------------------------------------------------------------------------- static guarantees

# Any call whose body can write a fact into the shared learnings space. Anything writing facts
# uses one of these HTTP verbs against an insight/ingest-shaped path (POST/PUT/DELETE/PATCH against
# `_request`) OR the PraxisClient equivalents (`add_insight`/`ingest`/`ingest_batch`/`save_snapshot`).
_WRITE_CALL_RE = re.compile(
    r"\b(add_insight|ingest_batch|save_snapshot)\s*\(|"
    r'_request\(\s*"(POST|PUT|PATCH|DELETE)"'
)
_FACTORY_LEARNINGS_REF_RE = re.compile(r"FACTORY_LEARNINGS_(SPACE|SNAPSHOT)|factory-learnings")


def _iter_py_files():
    for root in (AGENT_FACTORY_SRC, AGENT_FACTORY_HOOKS):
        yield from root.rglob("*.py")


def test_only_ingestion_api_writes_into_the_factory_learnings_space():
    """No source file other than ``ingestion_api.py`` may pair a write-shaped call with a reference
    to the factory-learnings space/snapshot in the same file (grep-testable sole-writer guarantee).
    ``_praxis.py`` itself is exempt: it only defines the generic, unscoped write primitive and the
    space/snapshot NAME constants — it never calls them together bound to a write."""
    offenders = []
    for path in _iter_py_files():
        if path.name in ("ingestion_api.py", "_praxis.py", "test_ingestion_api.py"):
            continue
        text = path.read_text(encoding="utf-8")
        if _WRITE_CALL_RE.search(text) and _FACTORY_LEARNINGS_REF_RE.search(text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], (
        f"only agent_factory.ingestion_api may write into the factory-learnings space; "
        f"found a write call alongside a factory-learnings reference in: {offenders}"
    )


_FILE_LESSON_READER_RE = re.compile(
    r"""(open\s*\([^)]*lesson|
         Path\([^)]*\)\s*\.\s*read_text\([^)]*\)\s*.{0,80}lesson|
         \.read_text\(\)[^\n]{0,80}lesson|
         json\.loads?\([^)]*lesson)""",
    re.IGNORECASE | re.VERBOSE,
)


def test_no_reader_loads_lessons_from_a_local_file_as_canonical():
    """Grep guard for KD1: lessons are cloud-canonical (Praxis) only. No reader anywhere in the
    factory package may load lesson content from a local file and treat it as the source of truth."""
    offenders = []
    for path in _iter_py_files():
        text = path.read_text(encoding="utf-8")
        if _FILE_LESSON_READER_RE.search(text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"file-backed lesson reader found (lessons must be cloud-canonical): {offenders}"


# --------------------------------------------------------------------------- R43: lesson source
# collision guard (a lesson's free-text ``source`` must never be shaped exactly like the
# ``prd-<project>`` grouping-tag convention ``Fact.source`` carries for requirement facts).

def _mock_insight_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub out the real HTTP write so these tests exercise only the guard, never the network."""
    monkeypatch.setattr(_praxis, "_request", lambda *a, **kw: {"id": "fake-id", "action": "added"})
    monkeypatch.setattr(_praxis, "ensure_space", lambda *a, **kw: a[0])


def test_write_lesson_rejects_a_prd_project_shaped_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """``source="prd-someproject"`` — the exact grouping-tag shape ``Fact.source`` carries for
    requirement facts (R43's acceptance) — is rejected on the first-write path."""
    _mock_insight_write(monkeypatch)
    with pytest.raises(ingestion_api.LessonSourceCollision):
        ingestion_api.write_lesson("some lesson text", source="prd-someproject")


def test_ingest_first_write_rejects_a_prd_project_shaped_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ingest()``'s first-write branch (no dedup match) routes through :func:`write_lesson`, so
    the guard fires there too, before any lesson is persisted."""
    _mock_insight_write(monkeypatch)
    monkeypatch.setattr(ingestion_api, "_require_authenticated", lambda identity=None: "tester")
    monkeypatch.setattr(ingestion_api, "classify_and_dedup",
                        lambda text, class_hint=None: {"duplicate_of": None, "class": "generic",
                                                        "content_hash": "h"})
    with pytest.raises(ingestion_api.LessonSourceCollision):
        ingestion_api.ingest("a fresh complaint never seen before", "some-project",
                             source="prd-some-project")


@pytest.mark.parametrize("benign_source", [
    None,
    "unit-test",
    "af-learn",
    "notes about prd conventions",   # mentions "prd" but isn't the exact grouping-tag shape
    "see docs/prd-notes.md for context",
])
def test_write_lesson_does_not_falsely_flag_a_merely_prd_mentioning_source(
    monkeypatch: pytest.MonkeyPatch, benign_source: str | None,
) -> None:
    """A source that merely MENTIONS "prd" in free text, without matching the exact
    ``prd-<project>`` shape, must not be flagged (R43's acceptance)."""
    _mock_insight_write(monkeypatch)
    ingestion_api.write_lesson("some lesson text", source=benign_source)  # must not raise
