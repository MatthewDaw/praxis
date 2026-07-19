#!/usr/bin/env python3
"""Mechanical plan-gate check — run ``agent_factory.plan_gate.evaluate_plan`` over the LIVE
``prd-<project>`` requirement facts and exit non-zero (naming every reason) if the plan is rejected.

This is the ENFORCED form of af-intake-plan's B6/B9 gate: the bless step runs this and cannot be
cleared while it exits non-zero, so plan_gate stops being skippable prose. It reads the exact same
fields the gate keys off — including ``meta.tags`` / ``meta.verify`` / ``meta.decision`` — so the
architecture-decision rules (recognized by tag OR the decision marker) fire on the live plan.

    python -m agent_factory.tools.plan_gate_check <project> [--out-of-scope c1,c2,...]

READ-ONLY: it only reads requirement facts; it never writes. Exit 0 = admitted, 1 = rejected (reasons
printed), 2 = could not run (Praxis unreachable, or no requirement facts for the project).

Import note: there are two ``agent_factory`` roots — the top-level namespace package (this ``tools/``
dir) and the regular ``src/agent_factory`` package (which holds ``plan_gate``/``gate``). They can't
both be on ``sys.path`` as one importable ``agent_factory``, so we file-load ``gate.py`` (pure stdlib)
and ``plan_gate.py`` under their canonical module names — robust however this tool is invoked.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent          # agent_factory/tools
_AF = _HERE.parent                               # agent_factory
_HOOKS = _AF / "hooks"
_SRC_AF = _AF / "src" / "agent_factory"

if str(_HOOKS) not in sys.path:
    sys.path.insert(0, str(_HOOKS))

import _praxis  # noqa: E402
from _praxis import PraxisUnreachable  # noqa: E402


def _load(modname: str, path: Path):
    """Import a module from an explicit file path, registering it under ``modname`` so a sibling's
    ``from <modname> import ...`` resolves to it (plan_gate imports ``agent_factory.gate``)."""
    spec = importlib.util.spec_from_file_location(modname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# gate.py is pure stdlib; register it as agent_factory.gate first so plan_gate's import resolves.
_load("agent_factory.gate", _SRC_AF / "gate.py")
_pg = _load("agent_factory.plan_gate", _SRC_AF / "plan_gate.py")
evaluate_plan = _pg.evaluate_plan
Requirement = _pg.Requirement


def _bare(project: str) -> str:
    p = str(project or "").strip()
    while p.startswith("prd-"):
        p = p[len("prd-"):]
    return p


def requirement_from_fact(fact: dict) -> Requirement:
    """Map ONE live ``prd-<project>`` requirement fact onto a plan-gate :class:`Requirement`.

    The gate keys ``id`` and ``depends_on`` on the ``requirement_id`` (e.g. "R8"), so we use
    ``meta.requirement_id`` as the id (falling back to the raw fact id). ``source`` is a top-level
    fact column; everything else lives in ``meta``. Crucially ``meta.decision`` is threaded through —
    dropping it here would re-open the tag-only hole the marker closes.
    """
    meta = fact.get("meta") or {}
    rid = str(meta.get("requirement_id") or fact.get("id") or fact.get("factId") or "")
    return Requirement(
        id=rid,
        text=str(fact.get("text") or fact.get("content") or ""),
        acceptance=str(meta.get("acceptance") or ""),
        defines=list(meta.get("defines") or []),
        references=list(meta.get("references") or []),
        source=str(fact.get("source") or meta.get("source") or ""),
        depends_on=[str(d) for d in (meta.get("depends_on") or [])],
        tags=list(meta.get("tags") or []),
        verify=str(meta.get("verify") or ""),
        decision=str(meta.get("decision") or ""),
    )


def check_plan(project: str, out_of_scope: list[str] | None = None):
    """Read the live plan and run the gate. Returns ``(verdict, requirements)``.

    Raises :class:`PraxisUnreachable` (fail-closed) if the facts can't be read, and ``ValueError`` if
    the project has NO requirement facts (a wrong project/org or empty plan must not silently "pass").
    """
    bare = _bare(project)
    facts = _praxis.facts_by(category="requirement", space=bare, snapshot=f"prd-{bare}")
    if not facts:
        raise ValueError(
            f"no requirement facts found for prd-{bare} (space={bare}). Wrong project/org, an "
            f"unblessed plan, or an empty snapshot — refusing to report a vacuous PASS."
        )
    requirements = [requirement_from_fact(f) for f in facts]
    verdict = evaluate_plan(requirements, out_of_scope=out_of_scope or [], project=bare)
    return verdict, requirements


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m agent_factory.tools.plan_gate_check",
        description="Run the plan done-gate over the LIVE prd-<project> facts; exit non-zero if the "
                    "plan is rejected. The ENFORCED (mechanical) form of af-intake-plan's bless gate.")
    p.add_argument("project", help="bare project name (or prd-<project>); reads snapshot prd-<project>")
    p.add_argument("--out-of-scope", default="",
                   help="comma-separated concepts declared out of scope (suppresses R-NO-DANGLING for them)")
    args = p.parse_args(argv)

    oos = [c.strip() for c in args.out_of_scope.split(",") if c.strip()]
    try:
        verdict, requirements = check_plan(args.project, out_of_scope=oos)
    except PraxisUnreachable as e:
        print(f"error: Praxis unreachable — cannot run the plan gate: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    bare = _bare(args.project)
    print(f"plan gate: prd-{bare}  ({len(requirements)} requirement(s))")
    if verdict.admitted:
        print("ADMITTED — the plan passes every mechanical rule.")
        return 0
    print(f"REJECTED — {len(verdict.reasons)} reason(s); the bless is BLOCKED until these are fixed:\n",
          file=sys.stderr)
    for r in verdict.reasons:
        print(f"  [{r.rule_id}] {r.message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
