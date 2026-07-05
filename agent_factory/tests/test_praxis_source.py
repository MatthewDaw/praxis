"""Unit tests for the eval's Praxis space lifecycle (evals/plan_repro/praxis_source.py).

No network: fake clients are injected. Covers the seed artifact, the create -> clear -> seed
-> read round trip, teardown, and the read paths (filtered get_context + preferred facts_by).
"""

from evals.plan_repro.praxis_source import (
    load_planning_checklist,
    load_seed_checklist,
    provision_and_load_checklist,
    teardown_eval_space,
)


class _FakeContextClient:
    """Read-only stand-in: serves canned hits via the semantic fallback."""

    def __init__(self, hits):
        self._hits = hits
        self.calls = []

    def get_context(self, query, top_k=8):
        self.calls.append((query, top_k))
        return {"hits": self._hits}


class _FakeFactsByClient:
    def __init__(self, facts):
        self._facts = facts

    def facts_by(self, category, scope):
        return {"facts": self._facts}

    def get_context(self, *a, **k):  # pragma: no cover - facts_by must win
        raise AssertionError("facts_by should be preferred over get_context")


class _FakeSpaceClient:
    """Full lifecycle stand-in: records create/clear and stores seeded facts for read-back."""

    def __init__(self):
        self.created = []
        self.cleared = 0
        self._store = []

    def create_space(self, space_id, name=""):
        self.created.append(space_id)
        return {"spaceId": space_id}

    def clear_graph(self):
        self.cleared += 1
        self._store = []
        return {"cleared": 0}

    def add_insight(self, insight, *, category=None, scope=None, source=None, on_conflict="auto_resolve"):
        self._store.append({"text": insight, "category": category, "scope": scope, "source": source})
        return {"id": "x"}

    def get_context(self, query, top_k=8):
        return {"hits": list(self._store)}


# --- read paths ----------------------------------------------------------------


def test_get_context_path_filters_to_planning_checks():
    hits = [
        {"text": "auth needs credential recovery", "category": "check", "scope": "planning"},
        {"text": "a login requirement", "category": "requirement", "scope": "mvp"},
        {"text": "a validation check", "category": "check", "scope": "validation"},
        {"text": "   ", "category": "check", "scope": "planning"},
    ]
    client = _FakeContextClient(hits)
    assert load_planning_checklist(client=client) == ["auth needs credential recovery"]
    assert client.calls


def test_facts_by_path_is_preferred_and_exhaustive():
    out = load_planning_checklist(client=_FakeFactsByClient([{"text": "lens one"}, {"text": "lens two"}, {"text": ""}]))
    assert out == ["lens one", "lens two"]


def test_empty_space_yields_empty_checklist():
    assert load_planning_checklist(client=_FakeContextClient([])) == []


# --- seed artifact -------------------------------------------------------------


def test_load_seed_checklist(tmp_path):
    art = tmp_path / "cl.yaml"
    art.write_text("checks:\n  - 'one'\n  - '   '\n  - 'two'\n", encoding="utf-8")
    assert load_seed_checklist(art) == ["one", "two"]


# --- lifecycle: create -> clear -> seed -> read --------------------------------


def test_provision_creates_clears_seeds_and_reads_back(tmp_path):
    art = tmp_path / "cl.yaml"
    art.write_text(
        "checks:\n  - 'auth needs credential recovery'\n  - 'screens need empty states'\n",
        encoding="utf-8",
    )
    fake = _FakeSpaceClient()
    out = provision_and_load_checklist(client=fake, space_id="eval-test", artifact=art)

    assert fake.created == ["eval-test"]      # created its own space
    assert fake.cleared == 1                  # cleared before seeding (clean slate)
    assert out == ["auth needs credential recovery", "screens need empty states"]  # round-tripped
    # seeded with the right tags so the read filter finds exactly these
    assert all(f["category"] == "check" and f["scope"] == "planning" for f in fake._store)


def test_teardown_clears_the_space():
    fake = _FakeSpaceClient()
    fake._store = [{"text": "x"}]
    teardown_eval_space(client=fake)
    assert fake.cleared == 1
    assert fake._store == []
