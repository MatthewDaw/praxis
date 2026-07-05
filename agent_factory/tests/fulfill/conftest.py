"""Shared fixtures for the af-fulfill runtime tests.

``DOMAIN_DIR`` points at the relocated proving pack; ``domain`` loads it once per test. The
malformed-pack helpers copy the real pack into a tmp dir and corrupt exactly one file, so the U1
error tests assert against realistic packs rather than hand-stubbed minimal ones.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOMAIN_DIR = REPO_ROOT / "domains" / "tax-1040-2025"


@pytest.fixture
def domain():
    from agent_factory.fulfill.domain import load_domain

    return load_domain(DOMAIN_DIR)


@pytest.fixture
def pack_factory(tmp_path):
    """Return a builder that clones the real pack into a tmp dir and applies a mutation.

    ``make(mutate)`` copies ``domains/tax-1040-2025`` to a fresh tmp dir, calls ``mutate(dir)`` to
    corrupt one file, and returns the dir path for ``load_domain``.
    """

    def make(mutate=None) -> Path:
        dest = tmp_path / "pack"
        shutil.copytree(DOMAIN_DIR, dest)
        if mutate is not None:
            mutate(dest)
        return dest

    return make


class FakeBackend:
    """In-memory, space-scoped stand-in for the Praxis graph the runtime writes to/reads from.

    Models exactly the derived gates the runtime depends on: completeness from recorded outcomes and
    bidirectional surface coverage from ``renders`` binds. Spaces are isolated (a keyed dict), so two
    sessions seeding two spaces never see each other's facts.
    """

    def __init__(self):
        self.spaces: dict[str, dict] = {}
        self._counter = 0

    def space(self, space_id: str) -> dict:
        return self.spaces.setdefault(
            space_id, {"reqs": {}, "surfaces": set(), "binds": set()}
        )

    def next_id(self) -> str:
        self._counter += 1
        return f"fact-{self._counter}"


class FakeFulfillPraxis:
    """Drop-in for :class:`FulfillPraxis` backed by :class:`FakeBackend`. ``space`` selects the graph."""

    def __init__(self, backend: FakeBackend, space: str | None = None):
        self.backend = backend
        self.space = space
        self.calls: list[tuple] = []

    # writes -----------------------------------------------------------------
    def create_space(self, space_id, name=None):
        self.calls.append(("create_space", space_id))
        self.backend.space(space_id)
        return {"spaceId": space_id}

    def delete_space(self, space_id):
        self.calls.append(("delete_space", space_id))
        self.backend.spaces.pop(space_id, None)
        return {"ok": True}

    def ensure_surface(self, project, screen_id, *, title=None):
        self.backend.space(self.space)["surfaces"].add(screen_id)
        return {"id": f"surf-{screen_id}"}

    def ingest_requirement(self, *, text, source, scope, meta):
        fid = self.backend.next_id()
        self.backend.space(self.space)["reqs"][fid] = {
            "id": fid, "text": text, "source": source, "scope": scope,
            "meta": dict(meta), "success": 0,
        }
        self.calls.append(("ingest", meta.get("requirement_id")))
        return fid

    def bind_surface(self, requirement_fact_id, screen_id, project, *, title=None):
        self.backend.space(self.space)["binds"].add((requirement_fact_id, screen_id))
        self.calls.append(("bind", requirement_fact_id, screen_id))
        return {"surfaceId": f"surf-{screen_id}"}

    def record_outcome(self, cid, success):
        for sp in self.backend.spaces.values():
            if cid in sp["reqs"]:
                sp["reqs"][cid]["success"] = 1 if success else sp["reqs"][cid]["success"]
        self.calls.append(("outcome", cid, success))
        return {"ok": True}

    # reads ------------------------------------------------------------------
    def _bare(self, project):
        while project.startswith("prd-"):
            project = project[len("prd-"):]
        return project

    def _project_reqs(self, project):
        source = f"prd-{self._bare(project)}"
        sp = self.backend.space(self.space)
        return [r for r in sp["reqs"].values() if r["source"] == source]

    def incomplete_requirements(self, project, *, exclude_leased=False):
        return [dict(r, claim={}) for r in self._project_reqs(project) if not r["success"]]

    def completeness_summary(self, project):
        reqs = self._project_reqs(project)
        complete = sum(1 for r in reqs if r["success"])
        return {"project": project, "total": len(reqs), "complete": complete,
                "incomplete": len(reqs) - complete}

    def surface_coverage(self, project, *, scope=None):
        sp = self.backend.space(self.space)
        bound_src = {src for src, _dst in sp["binds"]}
        bound_dst = {dst for _src, dst in sp["binds"]}
        uncovered_surfaces = [s for s in sp["surfaces"] if s not in bound_dst]
        uncovered_reqs = []
        for r in sp["reqs"].values():
            if scope is not None and r["meta"].get("scope") != scope:
                continue
            if r["id"] not in bound_src:
                uncovered_reqs.append(r)
        return {"uncoveredSurfaces": uncovered_surfaces, "uncoveredRequirements": uncovered_reqs}

    def get_fact(self, cid):
        for sp in self.backend.spaces.values():
            if cid in sp["reqs"]:
                return sp["reqs"][cid]
        return {}


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def fake_client(backend):
    return FakeFulfillPraxis(backend)


def rewrite_yaml(path: Path, transform) -> None:
    """Load ``path``'s YAML, apply ``transform(doc)`` (mutating in place or returning a new doc),
    and write it back."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    result = transform(doc)
    yaml.safe_dump(result if result is not None else doc, path.open("w", encoding="utf-8"))
