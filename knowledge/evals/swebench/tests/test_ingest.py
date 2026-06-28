"""Offline U3 tests: window selection, fix-restating exclusion, leakage guard, space isolation.

Runs fully offline — a fake fetcher serves fixture PR list/view/diff JSON and a fake
HTTP client records calls + returns canned responses. No real ``gh``, no real HTTP.
Per-instance isolation rides on SPACES within the fixed ``swebench_eval`` org.

    uv run pytest knowledge/evals/swebench/tests/test_ingest.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge.evals.swebench.instances import Instance, load_candidates
from knowledge.evals.swebench.ingest import (
    EVAL_ORG,
    IngestResult,
    LeakageError,
    OrgConflict,
    SpaceConflict,
    _batches,
    ensure_eval_org,
    ensure_space,
    fix_pr_number,
    ingest_window,
    leakage_guard,
    run_ingest,
    select_window,
    space_id_for,
    space_is_populated,
)

FIX = Path(__file__).parent / "fixtures"

# A space_id slug the backend accepts: lowercase letters/digits/dash/underscore.
_SPACE_SLUG = __import__("re").compile(r"^[a-z0-9_-]+$")


def _instances() -> dict[str, Instance]:
    records = json.loads((FIX / "rebench_sample.json").read_text(encoding="utf-8"))
    return {i.instance_id: i for i in load_candidates(records)}


# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------
def make_fake_fetcher(list_name: str = "pr_list.json"):
    """Serve fixture PR list/view/diff, switching on argv like the real fetcher."""
    pr_list = json.loads((FIX / list_name).read_text(encoding="utf-8"))
    views = json.loads((FIX / "pr_views.json").read_text(encoding="utf-8"))

    def fetch(argv: list[str]) -> str:
        # Strip an injected `-R <repo>` (make_repo_fetcher appends it) before matching.
        if "-R" in argv:
            i = argv.index("-R")
            argv = argv[:i] + argv[i + 2:]
        if argv[:3] == ["gh", "pr", "list"]:
            return json.dumps(pr_list)
        if argv[:3] == ["gh", "pr", "view"]:
            n = argv[3]
            return json.dumps(views[n]["view"])
        if argv[:3] == ["gh", "pr", "diff"]:
            n = argv[3]
            return views[n]["diff"]
        raise AssertionError(f"unexpected argv: {argv}")

    return fetch


class FakeClient:
    """Records every call; returns canned /ingest, /context, /graph responses per space.

    Tracks a fact count per space so ``get_graph`` reflects the reuse signal: a fresh space
    reads 0 nodes (ingest needed), and each ingested doc bumps it (2 facts/doc), so a
    second ``run_ingest`` sees it populated and reuses it.
    """

    org = EVAL_ORG

    def __init__(self, context_hits: dict | None = None, existing_spaces=(), populated=None):
        self.orgs: list[str] = []
        self.spaces: list[str] = list(existing_spaces)
        self.ingests: list[tuple[str, dict]] = []  # (space, body)
        self.context_hits = context_hits or {}  # space -> list[hit]
        self.facts: dict[str, int] = dict(populated or {})  # space -> active node count

    def post_orgs(self, body: dict) -> dict:
        org = body["orgId"]
        if org in self.orgs:
            raise OrgConflict(org)
        self.orgs.append(org)
        return {"orgId": org, "role": "owner"}

    def post_spaces(self, space_id: str, name: str | None = None) -> dict:
        if space_id in self.spaces:
            raise SpaceConflict(space_id)
        self.spaces.append(space_id)
        return {"spaceId": space_id, "name": name, "active": True}

    def post_ingest(self, space: str, body: dict) -> dict:
        self.ingests.append((space, body))
        docs = body.get("documents") or []
        self.facts[space] = self.facts.get(space, 0) + 2 * len(docs)  # canned 2 facts/doc
        # One result per document, in order — the real handler's contract (it loops docs).
        return {"results": [{"id": f"f{i}", "action": "ingested", "facts": 2,
                             "merged": 0, "conflicts": 0, "surfaced": 0}
                            for i in range(len(docs))], "count": len(docs)}

    def get_context(self, space: str, query: str, top_k: int) -> dict:
        return {"context": "", "hits": self.context_hits.get(space, [])}

    def get_graph(self, space: str, state: str = "active") -> dict:
        n = self.facts.get(space, 0)
        return {"graph": {"nodes": [{"id": f"n{i}"} for i in range(n)], "edges": []}}


# ---------------------------------------------------------------------------
# Space id slug.
# ---------------------------------------------------------------------------
def test_space_id_is_human_readable_valid_slug():
    inst = _instances()["sympy__sympy-fake-0001"]
    sid = space_id_for(inst)
    assert sid == "sympy__sympy-fake-0001"  # the instance id is already a valid, readable slug
    assert _SPACE_SLUG.fullmatch(sid)
    # An id with out-of-set chars (uppercase, slashes) is slugified to the allowed set.
    weird = Instance.from_record({"instance_id": "Foo/Bar.QUX", "repo": "x", "version": "1.13",
                                  "base_commit": "c", "created_at": "2025-01-01T00:00:00Z",
                                  "problem_statement": "", "patch": "", "test_patch": "",
                                  "FAIL_TO_PASS": [], "PASS_TO_PASS": [], "install_config": {}})
    assert _SPACE_SLUG.fullmatch(space_id_for(weird))


# ---------------------------------------------------------------------------
# Window selection + ordering.
# ---------------------------------------------------------------------------
def test_window_excludes_post_cutoff_and_orders_oldest_first():
    inst = _instances()["sympy__sympy-fake-0001"]  # cutoff 2025-03-10
    nums = select_window(inst, make_fake_fetcher())
    # 104 (2025-03-20) and 105 (2025-04-10) are at/after the cutoff → excluded.
    assert nums == [101, 102, 103]  # oldest-first by mergedAt


def test_fix_pr_excluded_by_number_even_without_restate():
    # The instance's gold PR is #102; it sits inside the date window but must be
    # dropped by number (the diff-restate guard could miss a squashed/rebased fix).
    inst = Instance.from_record({
        "instance_id": "sympy__sympy-102",
        "repo": "sympy/sympy", "version": "1.13",
        "base_commit": "deadbeef", "created_at": "2025-03-10T00:00:00Z",
        "problem_statement": "bug", "patch": "", "test_patch": "",
        "FAIL_TO_PASS": [], "PASS_TO_PASS": [], "install_config": {},
    })
    assert fix_pr_number(inst) == 102
    nums = select_window(inst, make_fake_fetcher())
    assert 102 not in nums  # fix-PR dropped by number
    assert nums == [101, 103]  # 101+103 are pre-cutoff; 102 dropped; 104/105 post-cutoff


def test_window_handles_mixed_timestamp_formats_at_boundary():
    # The real bug a live shakedown surfaced: SWE-rebench created_at is naive + space-
    # separated ("2025-03-10 13:52:59"), gh mergedAt is RFC3339 Z. A lexical compare
    # mis-orders them on the shared date ('T' sorts after ' '); parsed datetimes don't.
    inst = Instance.from_record({
        "instance_id": "sympy__sympy-9999",
        "repo": "sympy/sympy", "version": "1.13",
        "base_commit": "deadbeef", "created_at": "2025-03-10 13:52:59",
        "problem_statement": "bug", "patch": "", "test_patch": "",
        "FAIL_TO_PASS": [], "PASS_TO_PASS": [], "install_config": {},
    })

    def fetch(argv):
        if argv[:3] == ["gh", "pr", "list"]:
            return json.dumps([
                {"number": 201, "mergedAt": "2025-03-10T05:00:00Z", "title": "before cutoff (05:00 < 13:52)"},
                {"number": 202, "mergedAt": "2025-03-10T18:00:00Z", "title": "after cutoff (18:00 > 13:52)"},
            ])
        raise AssertionError(argv)

    nums = select_window(inst, fetch)
    # 201 merged 05:00 same day is BEFORE the 13:52 cutoff → kept; 202 at 18:00 → excluded.
    # A raw string compare would wrongly drop 201 ("...T05..." > "2025-03-10 13...").
    assert nums == [201]


# ---------------------------------------------------------------------------
# UrllibClient transient-failure retry (429/5xx → backoff, not crash).
# ---------------------------------------------------------------------------
def _http_error(url, code):
    import urllib.error

    return urllib.error.HTTPError(url, code, "x", {}, None)


def _fake_urlopen_sequence(*outcomes):
    """Build a urlopen stub that yields each outcome in turn: an int code raises that
    HTTPError; a bytes value is returned as a readable JSON response."""
    calls = {"n": 0}

    class _Resp:
        def __init__(self, data):
            self._data = data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._data

    def urlopen(req, *a, **k):
        outcome = outcomes[calls["n"]]
        calls["n"] += 1
        if isinstance(outcome, int):
            raise _http_error(req.full_url, outcome)
        return _Resp(outcome)

    return urlopen, calls


def test_urllib_client_retries_429_then_succeeds(monkeypatch):
    from knowledge.evals.swebench import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod.time, "sleep", lambda *_a, **_k: None)  # no real backoff wait
    urlopen, calls = _fake_urlopen_sequence(429, 503, b'{"results": [], "count": 0}')
    monkeypatch.setattr(ingest_mod.urllib.request, "urlopen", urlopen)

    client = ingest_mod.UrllibClient(base_url="http://x")
    out = client.post_ingest("space", {"documents": []})
    assert out == {"results": [], "count": 0}
    assert calls["n"] == 3  # two transient failures retried, third attempt succeeded


def test_urllib_client_does_not_retry_client_error(monkeypatch):
    from knowledge.evals.swebench import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod.time, "sleep", lambda *_a, **_k: None)
    urlopen, calls = _fake_urlopen_sequence(400, b'{"unreached": true}')
    monkeypatch.setattr(ingest_mod.urllib.request, "urlopen", urlopen)

    client = ingest_mod.UrllibClient(base_url="http://x")
    with pytest.raises(Exception) as exc_info:  # noqa: PT011 — HTTPError subclass
        client.get_graph("space")
    assert getattr(exc_info.value, "code", None) == 400
    assert calls["n"] == 1  # 4xx fails fast, no retry


def test_urllib_client_409_is_not_retried_and_becomes_space_conflict(monkeypatch):
    # 409 must NOT be retried and must still surface as SpaceConflict (idempotent create).
    from knowledge.evals.swebench import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod.time, "sleep", lambda *_a, **_k: None)
    urlopen, calls = _fake_urlopen_sequence(409, b'{"unreached": true}')
    monkeypatch.setattr(ingest_mod.urllib.request, "urlopen", urlopen)

    client = ingest_mod.UrllibClient(base_url="http://x")
    with pytest.raises(SpaceConflict):
        client.post_spaces("dup")
    assert calls["n"] == 1  # conflict resolved on the first response, never retried


# ---------------------------------------------------------------------------
# Batching (turn ~50 rate-limited per-PR posts into 1–2).
# ---------------------------------------------------------------------------
def test_batches_pack_to_cap_preserving_order():
    docs = [(1, "a" * 100), (2, "b" * 100), (3, "c" * 100)]
    # 100 + 100 = 200 <= 250; + 100 = 300 > 250 → [1, 2] then [3]. Order preserved.
    out = [[n for n, _ in b] for b in _batches(docs, max_bytes=250)]
    assert out == [[1, 2], [3]]


def test_batches_oversized_doc_is_yielded_alone():
    # A single doc bigger than the cap can't be split — it goes alone (server will 413 it),
    # never silently dropped, and the following doc starts a fresh batch.
    out = [[n for n, _ in b] for b in _batches([(1, "x" * 300), (2, "y" * 10)], max_bytes=250)]
    assert out == [[1], [2]]


def test_batches_counts_utf8_bytes_not_chars():
    # A 2-byte-per-char string fills the cap in half the characters.
    out = [[n for n, _ in b] for b in _batches([(1, "é" * 80), (2, "z" * 80)], max_bytes=200)]
    assert out == [[1], [2]]  # "é"*80 = 160 bytes; +80 = 240 > 200 → split


def test_fix_restating_pr_is_dropped_from_ingest():
    inst = _instances()["sympy__sympy-fake-0001"]
    client = FakeClient()
    result = ingest_window(inst, client, make_fake_fetcher())
    # 103's diff restates the gold empty-matrix guard → dropped; only 101, 102 ingested.
    assert result.pr_numbers == [101, 102]
    # Small fixture docs fit one batch → a SINGLE post carrying both, oldest-first.
    assert len(client.ingests) == 1
    posted = [d["source"] for _space, body in client.ingests for d in body["documents"]]
    assert posted == ["git/pr:101", "git/pr:102"]
    assert all(body["state"] == "active" for _space, body in client.ingests)
    # Every ingest posted under the instance's space, never another.
    assert {space for space, _ in client.ingests} == {space_id_for(inst)}


# ---------------------------------------------------------------------------
# Leakage guard.
# ---------------------------------------------------------------------------
def test_leakage_guard_raises_when_fact_restates_gold():
    inst = _instances()["sympy__sympy-fake-0001"]
    space = space_id_for(inst)
    leaked = {space: [{"id": "bad", "text": "the fix: return self._new(self.rows, other.cols, lambda i, j: 0)"}]}
    client = FakeClient(context_hits=leaked)
    with pytest.raises(LeakageError):
        leakage_guard(inst, client)


def test_leakage_guard_passes_on_clean_facts():
    inst = _instances()["sympy__sympy-fake-0001"]
    space = space_id_for(inst)
    clean = {space: [{"id": "ok", "text": "Matrix slicing was sped up by caching the index."}]}
    client = FakeClient(context_hits=clean)
    leakage_guard(inst, client)  # must not raise


def test_leakage_guard_ignores_trivial_gold_lines():
    # Regression for the sympy-27797 false positive: a gold patch's trivial change line
    # (a bare ``return``) appears in lots of fact text and must NOT trip the guard. Only a
    # SUBSTANTIVE gold line restated verbatim is a real leak. Gold here has both a
    # substantive line and a bare ``return``; the fact contains only the ``return``.
    from knowledge.evals.swebench.instances import Instance

    rec = {
        "instance_id": "sympy__sympy-trivial",
        "repo": "sympy/sympy", "version": "1.13", "base_commit": "c" * 40,
        "created_at": "2025-03-01T00:00:00Z",
        "problem_statement": "coefficient normalization is wrong",
        "patch": (
            "diff --git a/p.py b/p.py\n--- a/p.py\n+++ b/p.py\n@@ -1,2 +1,3 @@\n"
            "+        coeff = _normalize_coefficients(expr, domain)\n"
            "+        return\n"
        ),
        "test_patch": "", "FAIL_TO_PASS": [], "PASS_TO_PASS": [], "install_config": {},
    }
    inst = Instance.from_record(rec)
    space = space_id_for(inst)
    # fact contains the bare `return` (trivial) but NOT the substantive coeff line.
    hits = {space: [{"id": "x", "text": "def helper():\n        return\n"}]}
    client = FakeClient(context_hits=hits)
    leakage_guard(inst, client)  # must not raise — `return` is too trivial to be a leak


# ---------------------------------------------------------------------------
# org/space create idempotency.
# ---------------------------------------------------------------------------
def test_ensure_eval_org_and_space_swallow_409():
    inst = _instances()["sympy__sympy-fake-0001"]
    space = space_id_for(inst)
    client = FakeClient(existing_spaces=[space])
    ensure_eval_org(client)
    ensure_eval_org(client)  # second create 409s internally — swallowed, no raise
    assert client.orgs.count(EVAL_ORG) == 1
    ensure_space(client, space)  # already exists → 409 swallowed
    assert client.spaces.count(space) == 1


# ---------------------------------------------------------------------------
# Cross-instance isolation (R5) via spaces + ingestion-cost record present.
# ---------------------------------------------------------------------------
def test_two_instances_ingest_into_distinct_spaces_no_bleed():
    insts = _instances()
    a = insts["sympy__sympy-fake-0001"]   # cutoff 2025-03-10
    b = insts["sympy__sympy-fake-0003"]   # cutoff 2025-04-20 (later window)
    client = FakeClient()

    ra = run_ingest(a, client=client, fetch=make_fake_fetcher())
    rb = run_ingest(b, client=client, fetch=make_fake_fetcher())

    assert ra.space_id != rb.space_id
    # Each instance only ever posts under its own X-Praxis-Space — no cross bleed. Count
    # DOCUMENTS (PRs batch into fewer posts now), not posts, against the ingested PR count.
    docs_for_a = [d for s, body in client.ingests if s == ra.space_id for d in body["documents"]]
    docs_for_b = [d for s, body in client.ingests if s == rb.space_id for d in body["documents"]]
    assert len(docs_for_a) == ra.ingested
    assert len(docs_for_b) == rb.ingested
    # No ingest is posted to a space other than the two instance spaces — all under one org.
    assert {s for s, _ in client.ingests} == {ra.space_id, rb.space_id}
    assert client.orgs == [EVAL_ORG]  # the single fixed eval org, created once


def test_rerun_reuses_populated_space_without_reingesting():
    inst = _instances()["sympy__sympy-fake-0001"]
    client = FakeClient()

    first = run_ingest(inst, client=client, fetch=make_fake_fetcher())
    assert first.reused is False and first.ingested > 0
    posts_after_first = len(client.ingests)

    second = run_ingest(inst, client=client, fetch=make_fake_fetcher())
    assert second.reused is True
    assert second.ingested == 0
    assert second.facts_ingested == space_is_populated(client, space_id_for(inst))
    assert len(client.ingests) == posts_after_first  # NO new ingest posts on the rerun

    # reuse=False forces a fresh ingest even into a populated space.
    forced = run_ingest(inst, client=client, fetch=make_fake_fetcher(), reuse=False)
    assert forced.reused is False
    assert len(client.ingests) > posts_after_first


def test_ingestion_cost_field_present_per_instance():
    inst = _instances()["sympy__sympy-fake-0001"]
    client = FakeClient()
    result = ingest_window(inst, client, make_fake_fetcher())
    assert isinstance(result, IngestResult)
    # Field is present (placeholder None — /ingest surfaces counts, not cost).
    assert result.ingestion_cost is None
    assert hasattr(result, "ingestion_cost")
    assert result.facts_ingested == 2 * result.ingested  # canned 2 facts/doc
