"""U4 — requirement model, cover resolution, and Δ-bottom-line ranking.

Adapts raw Praxis requirement facts (or raw pack requirement dicts) to a typed
:class:`FulfillRequirement`, resolves each open requirement's cover strategy, and ranks the open set
by **materiality** — the max swing the bottom line takes across a requirement's plausible candidate
values, computed with the U2 ``what_if`` evaluator (S4). A requirement whose swing is below a
configurable threshold is marked *default-not-ask* (asking it cannot move the answer enough to be
worth a question from the S1 budget).

Mirrors ``build_target.py``: tolerant of shape drift (a missing tag yields a safe default, never a
crash) and fail-safe (a fact with no ``field`` routes to a triage bucket, never silently into the
ask set).
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any

from .domain import Domain
from .evaluator import bottom_line, evaluate

# Disposition of an open requirement after ranking.
TRIAGE = "triage"   # no field -> needs human attention, never asked
ASK = "ask"         # askable (guard + deps satisfied) and material enough to spend a question
DEFAULT = "default" # close by a policy default (immaterial, guard-failed, or budget fallback)
WAIT = "wait"       # dependency unmet and no default available -> not yet actionable

# Closed guard/predicate vocabulary (Q3).
_GUARD_OPS = {
    "exists": lambda have, present, _v: present,
    "eq": lambda have, present, v: present and have == v,
    "gt": lambda have, present, v: present and have > v,
    "lt": lambda have, present, v: present and have < v,
    "gte": lambda have, present, v: present and have >= v,
    "lte": lambda have, present, v: present and have <= v,
}


@dataclass(frozen=True)
class FulfillRequirement:
    """One requirement reduced to the fields the fulfill loop needs.

    Pulled from a Praxis fact's ``meta`` (the seeded shape) OR a raw pack requirement dict
    (top-level keys). A missing ``field`` is representable (``""``) and routes to triage rather than
    being coerced.
    """

    id: str
    field: str = ""
    verify: str = ""
    cover: list[str] = dc_field(default_factory=list)
    renders: list[str] = dc_field(default_factory=list)
    depends_on: list[str] = dc_field(default_factory=list)
    guard: dict[str, Any] | None = None
    scope: str = ""
    text: str = ""
    fact_id: str = ""

    @property
    def has_field(self) -> bool:
        return bool(self.field)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def requirement_from_fact(fact: dict) -> FulfillRequirement:
    """Adapt a raw Praxis requirement fact (or a pack requirement dict) into a typed requirement.

    ``meta`` keys win; top-level keys are the fallback (so a raw pack requirement parses too).
    Tolerant of a missing ``meta`` / missing keys — a fieldless requirement yields ``field=""`` and
    is routed to triage by :func:`rank_open`, never into the ask set.
    """
    meta = fact.get("meta") or {}

    def pick(key: str, default: Any = None) -> Any:
        if key in meta and meta[key] is not None:
            return meta[key]
        return fact.get(key, default)

    req_id = meta.get("requirement_id") or fact.get("id") or ""
    return FulfillRequirement(
        id=str(req_id),
        field=str(pick("field", "") or ""),
        verify=str(pick("verify", "") or ""),
        cover=_as_list(pick("cover")),
        renders=_as_list(pick("renders")),
        depends_on=_as_list(pick("depends_on")),
        guard=pick("guard") if isinstance(pick("guard"), dict) else None,
        scope=str(pick("scope", "") or ""),
        text=str(pick("text", "") or ""),
        fact_id=str(fact.get("id") or req_id),
    )


def _coerce(req: FulfillRequirement | dict) -> FulfillRequirement:
    return req if isinstance(req, FulfillRequirement) else requirement_from_fact(req)


def default_token(req: FulfillRequirement) -> str | None:
    """The ``default:*`` (or bare ``default``) token in this requirement's cover ladder, if any."""
    for token in req.cover:
        if token == "default" or token.startswith("default:"):
            return token
    return None


def resolve_cover(req: FulfillRequirement | dict, facts: dict, domain: Domain | None = None) -> str:
    """The recommended cover action for an OPEN requirement, walking its ``cover`` ladder in order.

    - a ``document:*`` / ``kg:*`` source is chosen iff the field is already present in ``facts``
      (a document already supplied it) — return that token so the loop records it as a doc cover;
    - ``user`` returns ``"ask"`` (the preferred action is to ask the taxpayer);
    - a ``default:*`` token returns itself (close by default).

    Falls back to ``"ask"`` when nothing in the ladder matched. Budget-driven downgrade from ask to
    the requirement's :func:`default_token` is the loop's job, not this pure resolver's.
    """
    req = _coerce(req)
    present = req.field in facts and facts[req.field] is not None
    for token in req.cover:
        if token.startswith("document:") or token.startswith("kg:"):
            if present:
                return token
            continue
        if token == "user":
            return "ask"
        if token == "default" or token.startswith("default:"):
            return token
    return "ask"


def _guard_ok(req: FulfillRequirement, facts: dict) -> bool:
    """True when the requirement's guard holds against known facts (or it has no guard).

    A guard whose field is absent FAILS (the requirement is not asked) — but the requirement can
    still be defaulted. Closed predicate set; an unrecognized op fails safe (not asked)."""
    if not req.guard:
        return True
    gfield = req.guard.get("field")
    op = req.guard.get("op")
    expected = req.guard.get("value")
    present = gfield in facts and facts[gfield] is not None
    have = facts.get(gfield)
    fn = _GUARD_OPS.get(op)
    if fn is None:
        return False
    return bool(fn(have, present, expected))


def deps_met(req: FulfillRequirement, domain: Domain, facts: dict) -> bool:
    """True when every ``depends_on`` prerequisite's field already has a value (S6: a readback only
    makes sense once the values it reads back exist)."""
    for dep_id in req.depends_on:
        dep = domain.requirement(dep_id)
        dep_field = (dep or {}).get("field") if isinstance(dep, dict) else None
        if dep_field and (dep_field not in facts or facts[dep_field] is None):
            return False
    return True


def _candidate_values(domain: Domain, req: FulfillRequirement) -> list[Any]:
    """The plausible value set used to measure materiality.

    An enum field is genuinely uncertain among its allowed values. A number/integer/boolean field
    carries no signal to vary from its default, so its candidate set is just the default — yielding
    a zero swing and a default-not-ask disposition (the single-W-2 ``other_income`` case)."""
    schema = domain.field_schemas.get(req.field, {})
    if schema.get("type") == "enum":
        return list(schema.get("values") or [])
    default = (domain.policy.get("defaults") or {}).get(req.field, {})
    if isinstance(default, dict) and "value" in default:
        return [default["value"]]
    return []


def materiality(domain: Domain, req: FulfillRequirement, facts: dict) -> float | None:
    """Max bottom-line swing across the requirement's candidate values (S4), via ``what_if``.

    Returns ``0.0`` when the swing is GENUINELY zero — fewer than two plausible candidates (e.g.
    ``other_income``, whose only candidate is its default), or candidates that all land on the same
    bottom line. Returns ``None`` (UNMEASURABLE) when there ARE ≥2 candidates but the bottom line is
    unknown for them because an UPSTREAM fact is still missing (e.g. ``filing_status`` before any
    wages are known). The distinction matters: a genuinely-immaterial requirement may be defaulted,
    but an unmeasurable one must NOT be — defaulting it would silently close a high-impact question
    on ignorance (a married/HoH taxpayer would get a Single return without ever being asked)."""
    candidates = _candidate_values(domain, req)
    if len(candidates) < 2:
        return 0.0
    outcomes: list[float] = []
    for value in candidates:
        results = evaluate(domain, facts, mode="what_if", overlay={req.field: value})
        bl = bottom_line(domain, results)
        if bl is not None:
            outcomes.append(bl)
    if len(outcomes) < 2:
        return None  # unmeasurable: an upstream fact is missing, not "immaterial"
    return max(outcomes) - min(outcomes)


@dataclass(frozen=True)
class RankedRequirement:
    req: FulfillRequirement
    materiality: float | None
    disposition: str


def rank_open(
    reqs: list[FulfillRequirement | dict],
    domain: Domain,
    facts: dict,
    *,
    threshold: float = 1.0,
) -> list[RankedRequirement]:
    """Rank the OPEN requirements for the next turn (S4).

    Each requirement gets a disposition: ``triage`` (no field), ``ask`` (guard + deps satisfied and
    materiality ≥ ``threshold``), ``default`` (immaterial / guard-failed but has a default), or
    ``wait`` (a dependency is unmet and there is no default to fall back on). The list is sorted
    ASK-first by descending materiality, so the loop pops the single most impactful question.
    """
    ranked: list[RankedRequirement] = []
    for raw in reqs:
        req = _coerce(raw)
        if not req.has_field:
            ranked.append(RankedRequirement(req, 0.0, TRIAGE))
            continue

        mat = materiality(domain, req, facts)
        has_default = default_token(req) is not None
        deps_ok = deps_met(req, domain, facts)
        guard_ok = _guard_ok(req, facts)

        if not deps_ok or not guard_ok:
            # Not askable this turn: close by default if we can, else wait for the prerequisite.
            disposition = DEFAULT if has_default else WAIT
        elif mat is None:
            # Unmeasurable (an upstream fact is missing) -> ASK; never default a high-impact
            # requirement on ignorance.
            disposition = ASK
        elif mat >= threshold:
            disposition = ASK
        elif has_default:
            # Askable but GENUINELY immaterial and a default exists -> don't spend a question.
            disposition = DEFAULT
        else:
            # Askable, immaterial, and nothing else covers it (e.g. a required readback) -> must ask.
            disposition = ASK
        ranked.append(RankedRequirement(req, mat, disposition))

    # ASK first (most material first), then DEFAULT/WAIT/TRIAGE; stable within each band. An
    # unmeasurable (None) materiality sorts as neutral within the ASK band.
    order = {ASK: 0, DEFAULT: 1, WAIT: 2, TRIAGE: 3}
    ranked.sort(key=lambda r: (order[r.disposition], -(r.materiality or 0.0)))
    return ranked
