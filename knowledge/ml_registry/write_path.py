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
* (R3a) a write carrying any field retired with the stored threshold is refused naming it
  (:func:`~knowledge.ml_registry.floor.guard_retired_threshold_fields`). Meta is merged
  verbatim, so a retired key nothing READS is still a retired key STORED, and a later
  reader cannot tell that fossil from a live bar.
* (R1) every patch against an ALREADY REGISTERED model goes through :func:`mutate_model`,
  which applies both of R1's mutation guards
  (:mod:`knowledge.ml_registry.guards`) on the data path itself rather than leaving the
  invariant to whichever caller remembers to check: a worker-sourced patch may not touch a
  protected judging field, and ``baseline`` moves only from ``source="adjudication"``.
  Registration (creating the model fact) is not a mutation and is unaffected.

:class:`RegistrySpace` is a JSON-persisted stand-in for the Praxis-backed "ml-research"
space: it gives the write path a real readback across process boundaries (the CLI below
persists it to a file) without requiring live Praxis infrastructure to prove this ticket's
acceptance condition. A Praxis-backed space need only expose the same ``insert``/``get``/
``list_facts`` surface.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from knowledge.ml_registry.citation import Resolver, resolve_citation
from knowledge.ml_registry.contracts.ledger_v2 import read_ledger_compatibility
from knowledge.ml_registry.guards import guard_baseline_move, guard_model_mutation
from knowledge.ml_registry.schema import (IDEA, MODEL, STATUS_SUPERSEDED,
                                          TERMINAL_TRIAL_STATUSES, TRIAL,
                                          RegistryValidationError, validate_fact)

SEEDED = "seeded"
DISCOVERED = "discovered"
IDEA_ORIGINS: tuple[str, ...] = (SEEDED, DISCOVERED)

MAX_DISCOVERED_IDEAS_FIELD = "max_discovered_ideas"

# The only value that means "unlimited discovered ideas". A caller who genuinely wants
# an unbounded discovery budget must SET this sentinel deliberately; it can never be
# arrived at by omitting the field (see DEFAULT_MAX_DISCOVERED_IDEAS below).
UNLIMITED_DISCOVERED_IDEAS = -1

# Campaign-budget fields a registration MAY omit (R11): each adopts this fixed default
# rather than being refused. Unlike the seven REQUIRED_META_KEYS[MODEL] fields, these
# never block registration by their absence.
MODEL_DEFAULTS: dict[str, object] = {
    "per_trial_seconds": 420,
    "max_trials": 200,
    MAX_DISCOVERED_IDEAS_FIELD: 8,
}

# A model fact carrying NO max_discovered_ideas at all (one built by some path other than
# register_model, which always populates it) falls back to the documented default rather
# than to "unlimited": a cost control must not be disabled by omission. Unlimited stays
# reachable, but only via the explicit UNLIMITED_DISCOVERED_IDEAS sentinel above.
DEFAULT_MAX_DISCOVERED_IDEAS = int(MODEL_DEFAULTS[MAX_DISCOVERED_IDEAS_FIELD])  # type: ignore[arg-type]

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
    return read_ledger_compatibility(path).commits


def register_model(space: RegistrySpace, meta: dict[str, object], *, model_id: str | None = None) -> str:
    """Register a model fact, or -- when ``model_id`` names an already-registered model --
    re-register (update) it in place. Returns the fact id (new for a fresh registration,
    ``model_id`` echoed back for an update).

    Every campaign-budget field in :data:`MODEL_DEFAULTS` (``per_trial_seconds``,
    ``max_trials``, ``max_discovered_ideas``) that the caller omits adopts its default
    before validation -- the ``REQUIRED_META_KEYS[MODEL]`` fields are unaffected and
    still refuse a registration missing any of them. The merged meta always runs through
    :func:`validate_fact`, defaulted or not.

    A model's ``metric`` is frozen for its life: re-registering against an existing
    ``model_id`` with a different ``metric`` is refused naming it, regardless of caller.
    """
    merged = dict(meta)
    for field_name, default in MODEL_DEFAULTS.items():
        merged.setdefault(field_name, default)
    # The rope's PROVENANCE against how trials are actually compared, and the shape of a
    # bar that MOVES with the metric level. Imported HERE rather than at module scope:
    # knowledge.ml_registry.floor imports this module, so a top-level import back would be
    # a cycle. They sit on register_model -- not only on
    # floor.register_model_with_baseline -- because this is the choke point every model
    # write passes, including the plain `praxis register-model` CLI path that checks
    # nothing else at all, and registration is the last moment before trials start burning
    # against the wrong bar. `sigmas` is checked here too, for the same reason: a value
    # that cannot be multiplied produces no bar at all.
    from knowledge.ml_registry.floor import (
        ROPE_SCALING_BASIS_FIELD,
        declared_adoption_floor,
        declared_sigmas,
        guard_retired_threshold_fields,
        guard_rope_provenance,
        guard_rope_scaling,
    )

    # Sits FIRST, and on this choke point rather than only on the baseline-backed path: a
    # retired field is refused before any of the guards below can read a stale meaning out
    # of it, and before the plain CLI registration -- which checks nothing else at all --
    # can store one. register_model_with_baseline delegates here, so all three registration
    # paths (plain, baseline-backed, re-registration) are closed by this one call.
    guard_retired_threshold_fields(merged)
    guard_rope_provenance(merged)
    declared_sigmas(merged)
    # The adoption floor is declared ONCE, before the baseline, like the rest of the judge --
    # so this is the moment an unusable one has to be refused. It is not a rope and is never
    # checked against one; it only has to be a positive number of metric points.
    declared_adoption_floor(merged)
    # The shape, its ceiling and its armor are the only things praxis can establish about a
    # bar that will be evaluated at levels no ledger row has reached, and the stamp says
    # which of them were actually established.
    merged[ROPE_SCALING_BASIS_FIELD] = guard_rope_scaling(merged)
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


def mutate_model(
    space: RegistrySpace, model_id: str, patch: dict[str, object], *, source: str
) -> Fact:
    """THE guarded mutation entry point for an ALREADY REGISTERED model fact.

    Every write that changes a registered model's meta belongs here rather than reaching
    into ``model.meta`` directly, so R1's two guards sit on the data path instead of on
    whichever caller happens to remember them:

    * :func:`~knowledge.ml_registry.guards.guard_model_mutation` -- refuses a
      ``source="worker"`` patch touching a protected judging field (``metric``,
      ``direction``, ``win_condition``, ``baseline_runs``, ``baseline_throughput``,
      ``diff_size_limit``). Other sources are not guarded here, matching R1's acceptance.
    * :func:`~knowledge.ml_registry.guards.guard_baseline_move` -- refuses ANY patch moving
      ``baseline`` whose ``source`` is not ``"adjudication"``.

    Adjudication (R10's ``verdict.adjudicate_verdict``, R12's ``floor``) is expected to
    route its baseline advance through here with ``source="adjudication"`` rather than
    assigning ``model.meta["baseline"]`` itself.

    Both guards run BEFORE anything is written, so a refused patch leaves the model
    untouched. Returns the updated model fact. Registration itself
    (:func:`register_model`) is not a mutation and does not pass through these guards.
    """
    model = space.get(model_id)
    if model is None or model.category != MODEL:
        raise RegistryValidationError(
            f"model {model_id!r} was never registered", field="model_id"
        )
    from knowledge.ml_registry.floor import guard_retired_threshold_fields, guard_rope_provenance

    guard_model_mutation(patch, source=source)
    guard_baseline_move(patch, source=source)
    # Against the PATCH, not the merged meta: a retired field cannot get past registration,
    # so the only way one arrives is in the patch itself. Checking the merge instead would
    # let a single legacy fact that already carries one wedge every unrelated patch against
    # that model forever, which retires nothing and breaks a live campaign.
    guard_retired_threshold_fields(patch)
    # Declaring the pairing AFTER registration must not be a way around the provenance
    # guard, so it is checked against the meta the patch would leave behind, not the
    # patch alone -- a patch naming only trial_comparison is exactly how a live campaign
    # would acquire the mismatch.
    guard_rope_provenance({**model.meta, **patch})
    model.meta.update(patch)
    return model


def discovered_idea_budget(model_meta: dict[str, object]) -> int:
    """The discovered-idea budget a model fact's meta implies.

    An ABSENT (or null) ``max_discovered_ideas`` falls back to
    :data:`DEFAULT_MAX_DISCOVERED_IDEAS` -- the documented registration default -- never to
    unlimited: a budget that silently disappears when its field is missing is a fail-open
    cost control. Unlimited remains reachable, but only when the caller deliberately sets
    the :data:`UNLIMITED_DISCOVERED_IDEAS` sentinel. Any OTHER negative value is refused
    rather than quietly read as "unlimited".
    """
    raw = model_meta.get(MAX_DISCOVERED_IDEAS_FIELD)
    if raw is None:
        return DEFAULT_MAX_DISCOVERED_IDEAS
    budget = int(raw)  # type: ignore[arg-type]
    if budget < 0 and budget != UNLIMITED_DISCOVERED_IDEAS:
        raise RegistryValidationError(
            f"{MAX_DISCOVERED_IDEAS_FIELD}={budget} is not a budget; use "
            f"{UNLIMITED_DISCOVERED_IDEAS} to ask for unlimited discovered ideas explicitly",
            field=MAX_DISCOVERED_IDEAS_FIELD,
        )
    return budget


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
        budget = discovered_idea_budget(model.meta)
        if budget != UNLIMITED_DISCOVERED_IDEAS:
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
    # Import locally while the legacy write-path module is being decomposed into domain
    # services; the service itself depends only on schema errors, not this module.
    from knowledge.ml_registry.services.ratchet import stamp_trial_lineage

    meta = dict(meta)
    validate_fact(TRIAL, meta)
    idea_id = str(meta["idea_id"])
    idea = space.get(idea_id)
    if idea is None or idea.category != IDEA:
        raise RegistryValidationError(
            f"trial references idea {idea_id!r} that was never registered", field="idea_id"
        )
    model_id = str(meta["model_id"])
    model = space.get(model_id)
    if model is None or model.category != MODEL:
        raise RegistryValidationError(
            f"trial references model {model_id!r} that was never registered", field="model_id"
        )
    stamp_trial_lineage(model_id, model.meta, meta)
    commit = str(meta["commit"])
    if commit not in ledger_commits:
        raise RegistryValidationError(
            f"trial commit {commit!r} has no matching row in the external ledger", field="commit"
        )
    counterfactual = meta.get("counterfactual_commit")
    if counterfactual is not None and str(counterfactual) not in ledger_commits:
        raise RegistryValidationError(
            f"counterfactual trial commit {counterfactual!r} has no matching row in the external ledger",
            field="counterfactual_commit",
        )

    # An idea may have only ONE trial in flight. A second one means the same question is being
    # answered twice concurrently, and the registry would happily adjudicate both -- producing two
    # verdicts for one idea, with whichever resolved last silently winning.
    #
    # Observed on the first real campaign: killing a supervising loop left its training child
    # orphaned to PID 1, still running the PREVIOUS (uncomposed) configuration. The relaunched
    # campaign started the composed arm under the same idea, so two runs raced -- each taking half
    # an 8-core box, and each about to write a ledger row under the same arm tag. Nothing in either
    # system objected. The duplicate was found by reading `ps`, which is not a control.
    #
    # This is not merely an operator error to be documented away: any long-running dispatcher that
    # is restarted after a crash can re-dispatch an arm whose previous run is still alive, and the
    # symptom (a verdict that does not match the configuration you think you measured) is close to
    # undiagnosable after the fact.
    #
    # A trial is IN FLIGHT until it has a verdict. Terminal statuses do not block, so a voided
    # trial can be re-run -- which is what voided MEANS -- and a resolved idea can be revisited by
    # a later ideation pass.
    in_flight = [
        f for f in space.list_facts(TRIAL)
        if str(f.meta.get("idea_id")) == idea_id and str(f.meta.get("status", "")) not in TERMINAL_TRIAL_STATUSES
    ]
    if in_flight:
        prior = in_flight[0]
        raise RegistryValidationError(
            f"idea {idea_id!r} already has trial {prior.id!r} in flight on commit "
            f"{prior.meta.get('commit')!r} (status {prior.meta.get('status')!r}); registering "
            f"{commit!r} as well would produce two verdicts for one idea. Resolve or void the "
            f"existing trial first -- or, if that run is genuinely dead, supersede it deliberately",
            field="idea_id",
        )
    return space.insert(TRIAL, meta, derived_from=(idea_id,))


def supersede_trial(space: RegistrySpace, trial_id: str, reason: str) -> str:
    """Mark an in-flight trial superseded so its idea can accept a new one.

    The deliberate escape hatch for :func:`register_trial`'s one-trial-in-flight rule. A run that
    died without resolving leaves its trial in flight forever, which would otherwise wedge the
    idea permanently -- so the rule needs a way out that is explicit rather than automatic.

    ``reason`` is required and recorded. A trial abandoned without a stated reason is
    indistinguishable from one that was quietly discarded for losing, and the dead-ideas register
    depends on that distinction.
    """
    trial = space.get(trial_id)
    if trial is None or trial.category != TRIAL:
        raise RegistryValidationError(
            f"trial {trial_id!r} was never registered", field="trial_id"
        )
    status = str(trial.meta.get("status", ""))
    if status in TERMINAL_TRIAL_STATUSES:
        raise RegistryValidationError(
            f"trial {trial_id!r} is already resolved ({status!r}); superseding it would rewrite "
            f"a verdict of record",
            field="trial_id",
        )
    if not reason.strip():
        raise RegistryValidationError("a supersede reason is required", field="reason")
    trial.meta["status"] = STATUS_SUPERSEDED
    trial.meta["superseded_reason"] = reason
    return trial_id


def resolve_idea_citation(
    space: RegistrySpace, idea_id: str, reference: str, resolver: Resolver
) -> dict[str, object]:
    """Resolve a registered idea's ``reference`` (R7) and record the outcome on it.

    Applies :func:`knowledge.ml_registry.citation.resolve_citation` against the idea's own
    prior ``unreachable_streak`` state (so a re-attempt on a later ideation pass continues
    the same consecutive-failure count), then merges the resulting patch onto the idea's
    meta in place -- ``basis``/``resolution``/``reference``/``reference_kind`` and, per
    outcome, ``title``/``authors``/``downgrade_note``/``unreachable_streak``. Returns the
    idea's updated meta.
    """
    idea = space.get(idea_id)
    if idea is None or idea.category != IDEA:
        raise RegistryValidationError(
            f"citation resolution references idea {idea_id!r} that was never registered", field="idea_id"
        )
    patch = resolve_citation(reference, idea.meta, resolver)
    idea.meta["reference"] = reference
    idea.meta.update(patch)
    return dict(idea.meta)
