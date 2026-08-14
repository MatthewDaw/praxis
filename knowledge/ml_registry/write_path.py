"""The registry write API: model / idea / trial registration (R2), plus R11's campaign
budgets and metric freeze on top of it.

Builds on R1's schema validation (:mod:`knowledge.ml_registry.schema`) to implement the
actual write path -- registering the three fact categories into a :class:`RegistrySpace`,
wiring the trial->idea ``derived_from`` edge, and enforcing the registry's data-integrity
rules that schema validation alone cannot express:

* every idea's ``origin`` is ``"seeded"`` or ``"discovered"``.
* a ``"discovered"`` idea beyond its model's ``max_discovered_ideas`` budget is refused,
  regardless of which caller made the request (unlike R1's worker-only mutation guards).
* a trial referencing an idea that was never registered is refused.
* a trial whose ``commit`` has no matching row in the external results ledger
  (``results.tsv``, written by the autoresearch loop -- see
  ``agent_factory/scripts/checks/af_ml_research_target.py``) is refused.
* (R11) a model registration omitting a campaign-budget field (``per_trial_seconds``,
  ``max_trials``, ``max_discovered_ideas``) adopts a fixed default rather than being
  refused -- see :data:`MODEL_DEFAULTS`; the seven judging fields R1 fixed as required
  are unaffected.
* (R11) a model's ``metric`` is frozen for its life -- re-registering against an existing
  ``model_id`` that tries to change it is refused naming it (:func:`register_model`).

:class:`RegistrySpace` is a JSON-persisted stand-in for the Praxis-backed "ml-research"
space: it gives the write path a real readback across process boundaries (the CLI below
persists it to a file) without requiring live Praxis infrastructure to prove this ticket's
acceptance condition. A Praxis-backed space need only expose the same ``insert``/``get``/
``list_facts`` surface.
"""

from __future__ import annotations

import csv
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from knowledge.ml_registry.schema import IDEA, MODEL, TRIAL, RegistryValidationError, validate_fact

SEEDED = "seeded"
DISCOVERED = "discovered"
IDEA_ORIGINS: tuple[str, ...] = (SEEDED, DISCOVERED)

MAX_DISCOVERED_IDEAS_FIELD = "max_discovered_ideas"
DEFAULT_MAX_DISCOVERED_IDEAS = -1  # no budget configured on the model -> unlimited

# Campaign-budget fields a registration MAY omit (R11): each adopts this fixed default
# rather than being refused. Unlike the seven REQUIRED_META_KEYS[MODEL] fields, these
# never block registration by their absence.
MODEL_DEFAULTS: dict[str, object] = {
    "per_trial_seconds": 420,
    "max_trials": 200,
    MAX_DISCOVERED_IDEAS_FIELD: 8,
}

METRIC_FIELD = "metric"


@dataclass
class Fact:
    id: str
    category: str
    meta: dict[str, object]
    derived_from: tuple[str, ...] = ()

    def to_json(self) -> dict[str, object]:
        return {"id": self.id, "category": self.category, "meta": self.meta, "derivedFrom": list(self.derived_from)}

    @classmethod
    def from_json(cls, raw: dict[str, object]) -> Fact:
        return cls(
            id=str(raw["id"]),
            category=str(raw["category"]),
            meta=dict(raw.get("meta") or {}),
            derived_from=tuple(raw.get("derivedFrom") or ()),
        )


@dataclass
class RegistrySpace:
    """The set of registered facts, keyed by id, plus their ``derived_from`` edges.

    A readback (:meth:`list_facts`) returns every fact registered so far -- the same
    guarantee a real Praxis space read gives via ``facts_by``.
    """

    facts: dict[str, Fact] = field(default_factory=dict)

    def insert(self, category: str, meta: dict[str, object], *, derived_from: tuple[str, ...] = ()) -> str:
        fact_id = f"{category}-{uuid.uuid4().hex[:12]}"
        self.facts[fact_id] = Fact(id=fact_id, category=category, meta=dict(meta), derived_from=derived_from)
        return fact_id

    def get(self, fact_id: str) -> Fact | None:
        return self.facts.get(fact_id)

    def replace(self, fact_id: str, meta: dict[str, object]) -> None:
        """Overwrite an EXISTING fact's meta in place, keeping its id/category/edges."""
        existing = self.facts[fact_id]
        self.facts[fact_id] = Fact(
            id=fact_id, category=existing.category, meta=dict(meta), derived_from=existing.derived_from
        )

    def list_facts(self, category: str | None = None) -> list[Fact]:
        facts = list(self.facts.values())
        return [f for f in facts if f.category == category] if category is not None else facts

    def to_json(self) -> dict[str, object]:
        return {"facts": [f.to_json() for f in self.facts.values()]}

    @classmethod
    def from_json(cls, raw: dict[str, object]) -> RegistrySpace:
        space = cls()
        for entry in raw.get("facts") or []:
            fact = Fact.from_json(entry)
            space.facts[fact.id] = fact
        return space

    @classmethod
    def load(cls, path: Path) -> RegistrySpace:
        if not path.exists():
            return cls()
        return cls.from_json(json.loads(path.read_text()))

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_json(), indent=2))


def load_ledger_commits(path: Path) -> frozenset[str]:
    """Read the ``commit`` column of the autoresearch loop's ``results.tsv``.

    Same file/format ``af_ml_research_target.py`` reads (header
    ``commit\tval_bpb\tmemory_gb\tstatus\tdescription``); a trial is only real if its
    commit has a matching row here.
    """
    with path.open(newline="") as fh:
        reader = csv.reader(fh, delimiter="\t")
        try:
            next(reader)  # header
        except StopIteration:
            return frozenset()
        return frozenset(row[0].strip() for row in reader if row and row[0].strip())


def register_model(space: RegistrySpace, meta: dict[str, object], *, model_id: str | None = None) -> str:
    """Register a model fact, or -- when ``model_id`` names an already-registered model --
    re-register (update) it in place. Returns the fact id (new for a fresh registration,
    ``model_id`` echoed back for an update).

    Every campaign-budget field in :data:`MODEL_DEFAULTS` (``per_trial_seconds``,
    ``max_trials``, ``max_discovered_ideas``) that the caller omits adopts its default
    before validation -- the seven ``REQUIRED_META_KEYS[MODEL]`` fields are unaffected and
    still refuse a registration missing any of them. The merged meta always runs through
    :func:`validate_fact`, defaulted or not.

    A model's ``metric`` is frozen for its life: re-registering against an existing
    ``model_id`` with a different ``metric`` is refused naming it, regardless of caller.
    """
    merged = dict(meta)
    for field_name, default in MODEL_DEFAULTS.items():
        merged.setdefault(field_name, default)
    validate_fact(MODEL, merged)

    if model_id is None:
        return space.insert(MODEL, merged)

    existing = space.get(model_id)
    if existing is None or existing.category != MODEL:
        raise RegistryValidationError(
            f"model {model_id!r} was never registered", field="model_id"
        )
    if merged.get(METRIC_FIELD) != existing.meta.get(METRIC_FIELD):
        raise RegistryValidationError(
            f"model {model_id!r} metric is frozen for the life of the model; "
            f"cannot change it from {existing.meta.get(METRIC_FIELD)!r} to {merged.get(METRIC_FIELD)!r}",
            field=METRIC_FIELD,
        )
    space.replace(model_id, merged)
    return model_id


def register_idea(space: RegistrySpace, meta: dict[str, object]) -> str:
    """Register an idea fact.

    Refuses an ``origin`` outside ``seeded``/``discovered``, and refuses a
    ``"discovered"`` idea once its model's ``max_discovered_ideas`` budget is already
    met -- no matter which caller made the request.
    """
    validate_fact(IDEA, meta)
    origin = meta.get("origin")
    if origin not in IDEA_ORIGINS:
        raise RegistryValidationError(
            f"idea origin must be one of {IDEA_ORIGINS}, got {origin!r}", field="origin"
        )
    if origin == DISCOVERED:
        model_id = str(meta["model_id"])
        model = space.get(model_id)
        if model is None or model.category != MODEL:
            raise RegistryValidationError(
                f"idea references model {model_id!r} that was never registered", field="model_id"
            )
        budget = model.meta.get(MAX_DISCOVERED_IDEAS_FIELD, DEFAULT_MAX_DISCOVERED_IDEAS)
        budget = int(budget) if budget is not None else DEFAULT_MAX_DISCOVERED_IDEAS
        if budget >= 0:
            already = sum(
                1
                for f in space.list_facts(IDEA)
                if f.meta.get("model_id") == model_id and f.meta.get("origin") == DISCOVERED
            )
            if already >= budget:
                raise RegistryValidationError(
                    f"model {model_id!r} discovered-idea budget max_discovered_ideas={budget} is exhausted",
                    field=MAX_DISCOVERED_IDEAS_FIELD,
                )
    return space.insert(IDEA, meta)


def register_trial(space: RegistrySpace, meta: dict[str, object], ledger_commits: frozenset[str]) -> str:
    """Register a trial fact, wiring a ``derived_from`` edge naming its idea's fact id.

    Refuses a trial whose idea was never registered, and refuses a trial whose commit
    has no matching row in ``ledger_commits`` (the external results ledger).
    """
    validate_fact(TRIAL, meta)
    idea_id = str(meta["idea_id"])
    idea = space.get(idea_id)
    if idea is None or idea.category != IDEA:
        raise RegistryValidationError(
            f"trial references idea {idea_id!r} that was never registered", field="idea_id"
        )
    commit = str(meta["commit"])
    if commit not in ledger_commits:
        raise RegistryValidationError(
            f"trial commit {commit!r} has no matching row in the external ledger", field="commit"
        )
    return space.insert(TRIAL, meta, derived_from=(idea_id,))
