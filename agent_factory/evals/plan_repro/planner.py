"""The planner-under-test — produces a candidate plan from the raw PRD, to score for holes.

See ``docs/coverage-spine/03-eval-agent.md`` / ``02-planner.md``. The plan-reproduction eval
needs a *controllable* planner so it can measure the hole rate and A/B the planning checklist:

- **baseline** (`checklist=None`): plan straight from the PRD prose.
- **treatment** (`checklist=` the planning checklist loaded from Praxis): apply general lenses.

The delta in `derived`-feature holes between the two is the meta-proof that the checklist
closes holes. This is a deliberately controllable proxy for the production gated planner
(`af-intake`/`af-plan`), not a replacement — it isolates one variable (the checklist).

The checklist is NOT hard-coded here: it is loaded from the Praxis knowledge graph at
execution time (see :mod:`evals.plan_repro.praxis_source`), so the eval relies on Praxis as the
single source of truth — never a private copy of the checks. Like :mod:`llm_evaluator`, the
model is injected as ``Complete = (prompt) -> text`` so this is testable without a network.

IMPORTANT: the Praxis checklist is **general lenses** ("apps with auth need credential
recovery"), NOT the golden feature list. It encodes reusable engineering knowledge; applying
it to *this* PRD is what should surface the password-reset / consent / empty-state features.
The golden is the answer key (for scoring only); the checklist must never be the answer key.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

import yaml

from evals.plan_repro.coverage import Feature

#: Same contract as the evaluator's backend.
Complete = Callable[[str], str]

_REPO_ROOT = Path(__file__).resolve().parents[2]
#: The raw PRD set the planner reproduces from.
DEFAULT_PRD_DIR = _REPO_ROOT / "docs" / "inspiration"
DEFAULT_GOLDEN = Path(__file__).resolve().parent / "team-app" / "golden-features.yaml"

# The planning checklist is NOT defined here — it lives in Praxis and is loaded at execution
# time via evals.plan_repro.praxis_source.load_planning_checklist(). Keeping a copy in code
# would defeat the point (the eval must rely on Praxis as the single source of truth).


# --- PRD loading ---------------------------------------------------------------


def load_prd(paths: list[str | Path] | None = None) -> str:
    """Read + concatenate the PRD docs (default: every ``*.txt`` in docs/inspiration/)."""
    if paths is None:
        files = sorted(DEFAULT_PRD_DIR.glob("*.txt"))
    else:
        files = [Path(p) for p in paths]
    chunks = []
    for f in files:
        chunks.append(f"===== {f.name} =====\n{f.read_text(encoding='utf-8')}")
    return "\n\n".join(chunks)


# --- prompt --------------------------------------------------------------------


def build_planner_prompt(prd_text: str, *, checklist: list[str] | None = None) -> str:
    """Build the 'enumerate the complete feature set' prompt for the planner-under-test."""
    lens_block = ""
    if checklist:
        lenses = "\n".join(f"  - {c}" for c in checklist)
        lens_block = (
            "\n\nApply EACH of these general engineering considerations and include any "
            "feature they imply for THIS product (only if the product warrants it):\n"
            f"{lenses}\n"
        )
    return (
        "You are the planner for a software factory. From the product docs below, enumerate "
        "the COMPLETE feature set needed to ship a production-ready MVP (plus any clearly "
        "post-MVP features the docs imply).\n\n"
        "Output one atomic feature per item — a single capability or behavior, phrased like a "
        "requirement (e.g. 'a user can reset their password via an emailed link'). Be "
        "exhaustive: think past the happy path to recovery flows, permissions, states, "
        "admin tooling, and edge cases a contractor would otherwise miss.\n\n"
        "Systematically cover, wherever the product implies them, each of these (each missing one is a "
        "hole):\n"
        "  - EVERY entry path: first-user/self-registration, invited NEW-user signup, AND an existing/"
        "authenticated user redeeming an invite or joining an additional group/team.\n"
        "  - FULL CRUD for every user-created entity: create, list/history, edit, AND delete/retract/undo.\n"
        "  - An account/profile surface for EACH role, plus a settings surface for each owner/admin role.\n"
        "  - A read/view surface for EVERY piece of content or identity any role authors or configures.\n"
        "  - An explicit success/confirmation state for every consequential action.\n"
        "  - The exact definition/formula/threshold for every metric, streak, status, rule, or qualifying "
        "condition the product names.\n"
        "  - Every explicit PROHIBITION / must-not rule (e.g. no public ranking, no sensitive data).\n\n"
        "ATOMICITY — avoid oversplitting. Each item is ONE independently-buildable capability, not a "
        "fragment. A definition, constraint, formula, threshold, or prohibition belongs ON the feature it "
        "governs — do NOT emit it as its own item unless it is independently buildable. Fold qualifiers "
        "into their feature (e.g. 'only one theme per week / latest wins' is part of 'create a weekly "
        "theme'; 'completed means prompt+ratings submitted' is part of the completion feature; a row's "
        "uniqueness constraint is part of the submit feature). BUT keep genuinely DISTINCT capabilities "
        "as their OWN items — fold constraints/definitions, NEVER fold away a distinct capability. "
        "Distinct = a different user ACTION, a different ENTRY PATH (self-register that bootstraps a team "
        "vs invited new-user signup vs an existing user redeeming an invite are THREE features), a "
        "distinct CRUD operation (delete/retract is its own capability, not part of create), or a "
        "separate user-facing SURFACE (an athlete account screen is its own feature). Aim for the "
        "smallest set that still names every distinct capability — completeness of capability, not "
        "maximal splitting and not over-merging."
        f"{lens_block}\n"
        "PRODUCT DOCS:\n"
        f"{prd_text}\n\n"
        'Respond with JSON only: a list of {"id":"R1","text":"<feature>","scope":"mvp|post-mvp"}.'
    )


# --- parsing -------------------------------------------------------------------


def _loads_lenient(text: str):
    """Parse the first top-level JSON value (array or object) out of model text."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    starts = [p for p in (s.find("["), s.find("{")) if p != -1]
    if not starts:
        return None
    start = min(starts)
    open_ch = s[start]
    close_ch = "]" if open_ch == "[" else "}"
    depth = 0
    for i in range(start, len(s)):
        if s[i] == open_ch:
            depth += 1
        elif s[i] == close_ch:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start : i + 1])
                except Exception:
                    return None
    return None


def parse_candidate(raw: str | list | dict) -> list[Feature]:
    """Parse a planner response into candidate :class:`Feature` items (tolerant)."""
    obj = raw if isinstance(raw, (list, dict)) else _loads_lenient(raw)
    if isinstance(obj, dict):
        items = obj.get("features") or obj.get("requirements") or []
    elif isinstance(obj, list):
        items = obj
    else:
        items = []
    out: list[Feature] = []
    for i, it in enumerate(items):
        if isinstance(it, str):
            out.append(Feature(id=f"C{i}", text=it.strip()))
        elif isinstance(it, dict):
            text = str(it.get("text", it.get("feature", ""))).strip()
            meta = {k: v for k, v in it.items() if k not in ("id", "text", "feature")}
            out.append(Feature(id=str(it.get("id", f"C{i}")), text=text, meta=meta))
    return [f for f in out if f.text]


def build_gate_prompt(
    prd_text: str, candidate: list[Feature], *, checklist: list[str] | None = None
) -> str:
    """A COMPLETENESS-GATE pass: audit a candidate plan and emit only the MISSING distinct capabilities.

    This is the structural forcing function (harder than a prompt lens): the plan does not pass until an
    independent gate confirms every coverage dimension is represented by a distinct, buildable feature.
    It returns ADDITIONS only — and is told to respect atomicity (add distinct capabilities, never
    constraints/definitions), so it recovers genuine holes without re-bloating the plan.
    """
    listing = "\n".join(f"  - {f.text}" for f in candidate)
    lens_block = ""
    if checklist:
        lens_block = "\n\nCoverage lenses to audit against:\n" + "\n".join(f"  - {c}" for c in checklist)
    return (
        "You are a COMPLETENESS GATE auditing a candidate feature plan against a product spec. Your ONLY "
        "job is to find DISTINCT capabilities the product implies that the plan is MISSING (or folded away "
        "so they're no longer represented). For each coverage dimension — every ENTRY PATH (self-register/"
        "bootstrap, invited new-user signup, existing-user invite redemption), full CRUD per user-created "
        "entity (create, list/history, edit, delete/retract), an account/profile surface per role, a "
        "read/view surface for every authored content type, every named PROHIBITION, and the exact "
        "definition of every named rule/metric — check whether a DISTINCT feature covers it.\n\n"
        "Rules: only add a feature for a genuinely DISTINCT, independently-buildable capability that is "
        "absent (a different action, entry path, CRUD op, or surface). Do NOT re-add things already "
        "present, do NOT add constraints/definitions (those belong on their feature), do NOT invent "
        "out-of-scope features."
        f"{lens_block}\n\n"
        "PRODUCT DOCS:\n" + prd_text + "\n\n"
        "CANDIDATE PLAN:\n" + listing + "\n\n"
        'Respond with JSON only: a list of {"id":"A1","text":"<missing distinct feature>","scope":"mvp|'
        'post-mvp"} — the ADDITIONS only. Empty list [] if the plan is already complete.'
    )


def gate_candidate(
    complete: Complete, prd_text: str, candidate: list[Feature], *, checklist: list[str] | None = None
) -> list[Feature]:
    """Run one completeness-gate pass; return the candidate augmented with any missing distinct features."""
    additions = parse_candidate(complete(build_gate_prompt(prd_text, candidate, checklist=checklist)))
    seen = {f.text.strip().lower() for f in candidate}
    fresh = [f for f in additions if f.text.strip().lower() not in seen]
    return candidate + fresh


def produce_candidate(
    complete: Complete, prd_text: str, *, checklist: list[str] | None = None, gate_rounds: int = 2
) -> list[Feature]:
    """Run the planner-under-test: PRD (+ optional checklist) -> candidate, then ENFORCE a completeness
    gate. The gate (``gate_rounds`` passes, stopping early when a pass adds nothing) is the structural
    forcing function: the plan isn't accepted until the gate finds no missing distinct capability."""
    candidate = parse_candidate(complete(build_planner_prompt(prd_text, checklist=checklist)))
    for _ in range(max(0, gate_rounds)):
        augmented = gate_candidate(complete, prd_text, candidate, checklist=checklist)
        if len(augmented) == len(candidate):  # gate is clean -> accept
            break
        candidate = augmented
    return candidate


# --- persistence ---------------------------------------------------------------


def save_candidate(features: list[Feature], path: str | Path, *, project: str = "") -> None:
    """Write a candidate plan in the shape :func:`coverage.load_candidate` reads."""
    payload = {
        "project": project,
        "features": [
            {"id": f.id, "text": f.text, **({"meta": f.meta} if f.meta else {})}
            for f in features
        ],
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
