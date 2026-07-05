"""Event-log escape harvester — mines false-admit escapes into draft RED cases (U6).

The factory's compounding loop closes here: when the plan done-gate *admits* a task
(``gate_result: admitted``) but the task later *fails* (``outcome: failed``) for the
same ``task_id``, the gate let an escape through. That pairing is a falsifiable signal
that the suite is missing a case. :func:`harvest` reads a run's event log, finds those
pass-then-fail pairs, and scaffolds a draft ``case.yaml`` into ``evals/cases/_quarantine/``
seeded with the offending gate input and a ``red_proof`` that points back at the
originating events. The draft lands with ``status: proposed`` so it is inert until a
human ratifies it (KTD6) — promotion is moving the file out of quarantine.

Design rules (the key technical decisions this unit implements):
- **False-admits only (KTD2).** Only an *admitted* gate paired with a *later failed*
  outcome is an escape. A rejected gate (false-reject) never produces a harvestable
  outcome and is structurally out of scope — it is never harvested.
- **RED-proof references the originating event (KTD3).** The draft's ``red_proof`` is a
  ``harvested`` record carrying the run id, task id, and the seqs of the gate_result and
  outcome events, so the evidence is auditable back to the log line that produced it.
- **Idempotent on a stable signature.** Re-running over the same log adds nothing: the
  draft's identity (and filename) is derived from ``(run_id, task_id, gate_result_seq)``,
  and an existing draft is left untouched.
- **Correlate on ``task_id`` (the frozen gate-emission key).** ``emit_gate_result`` writes
  ``task_id``; the failing ``outcome`` shares it. The gate *input* is not on the frozen
  ``gate_result`` event, so it is recovered from a correlated event (same ``task_id``)
  that carries an ``input`` block.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent_factory.event_log import EventLog

#: Where ratifiable draft cases land. Discovered like any other case dir, but each draft
#: carries ``status: proposed`` so the green-locking runner skips it until promoted.
DEFAULT_QUARANTINE_DIR: Path = Path(__file__).resolve().parent / "cases" / "_quarantine"

_UNSAFE_NAME_RE = re.compile(r"[^0-9A-Za-z._-]+")


@dataclass(frozen=True)
class Escape:
    """One harvested false-admit escape: an admitted gate that a later outcome falsified.

    ``input`` is the offending gate input recovered from a correlated event (empty if the
    log carried none). ``red_proof`` is the ``harvested`` evidence record pointing back at
    the originating ``gate_result``/``outcome`` seqs.
    """

    case_id: str
    component: str
    task_id: str
    input: dict = field(default_factory=dict)
    red_proof: dict = field(default_factory=dict)


def harvest(run_dir: str | Path, *, quarantine_dir: str | Path = DEFAULT_QUARANTINE_DIR) -> list[Path]:
    """Harvest false-admit escapes from ``run_dir`` into draft cases under ``quarantine_dir``.

    Reads ``run_dir/events.jsonl`` via :meth:`EventLog.read`, pairs each admitted
    ``gate_result`` with a *later* failed ``outcome`` for the same ``task_id``, and writes a
    ``status: proposed`` draft ``case.yaml`` per escape. Returns the list of newly-written
    case-file paths. Idempotent: an escape whose draft already exists is skipped, so a
    re-run over the same log returns ``[]``.
    """
    run_dir = Path(run_dir)
    log = EventLog(run_dir.name, root=run_dir.parent)
    events = log.read()

    quarantine_dir = Path(quarantine_dir)
    created: list[Path] = []
    for escape in _find_escapes(events):
        case_path = quarantine_dir / escape.case_id / "case.yaml"
        if case_path.exists():
            continue  # already harvested — dedupe on the stable signature (filename)
        case_path.parent.mkdir(parents=True, exist_ok=True)
        case_path.write_text(_render_case_yaml(escape), encoding="utf-8")
        created.append(case_path)
    return created


def _find_escapes(events: list[dict[str, Any]]) -> list[Escape]:
    """Pair admitted ``gate_result`` events with later failed ``outcome`` events.

    Scans in log order: an admitted gate is remembered by ``task_id``; a subsequent failed
    outcome on the same task forms an escape. Dedupes within the run on the stable
    ``(run_id, task_id, gate_result_seq)`` signature so a task that fails twice yields one
    draft. False-rejects (``admitted`` false) are never recorded, so are never harvested.
    """
    passed_gates: dict[Any, dict[str, Any]] = {}
    seen: set[tuple[Any, Any, Any]] = set()
    escapes: list[Escape] = []

    for ev in events:
        etype = ev.get("type")
        if etype == "gate_result" and ev.get("admitted"):
            # First admitted gate per task is the one that let the escape through.
            passed_gates.setdefault(ev.get("task_id"), ev)
        elif etype == "outcome" and _outcome_failed(ev):
            task_id = ev.get("task_id")
            gate = passed_gates.get(task_id)
            if gate is None:
                continue  # no admitted gate for this task — not a false-admit
            if ev.get("seq", 0) <= gate.get("seq", 0):
                continue  # outcome must be strictly LATER than the gate it falsifies
            signature = (ev.get("run_id"), task_id, gate.get("seq"))
            if signature in seen:
                continue
            seen.add(signature)
            escapes.append(_build_escape(events, gate, ev))
    return escapes


def _build_escape(events: list[dict[str, Any]], gate: dict[str, Any], outcome: dict[str, Any]) -> Escape:
    component = gate.get("component", "plan_gate")
    task_id = gate.get("task_id")
    run_id = gate.get("run_id")
    gate_seq = gate.get("seq")
    outcome_seq = outcome.get("seq")

    signature = f"{run_id}|{task_id}|{gate_seq}"
    digest = hashlib.sha1(signature.encode("utf-8")).hexdigest()[:10]
    safe_tid = _UNSAFE_NAME_RE.sub("_", str(task_id)) or "task"
    case_id = f"harvested_{component}_{safe_tid}_{digest}"

    red_proof = {
        "kind": "harvested",
        "run_id": run_id,
        "task_id": task_id,
        "gate_result_seq": gate_seq,
        "outcome_seq": outcome_seq,
    }
    return Escape(
        case_id=case_id,
        component=component,
        task_id=str(task_id),
        input=_recover_gate_input(events, gate),
        red_proof=red_proof,
    )


def _recover_gate_input(events: list[dict[str, Any]], gate: dict[str, Any]) -> dict:
    """Recover the offending gate input for the escaped task.

    The frozen ``gate_result`` event does not carry the input, so it is read from the
    nearest event at/before the gate that shares the task's ``task_id`` and carries an
    ``input`` block (a ``plan``/``task_start``/``decision`` the component evaluated). Falls
    back to an ``input`` field on the gate event itself, then to ``{}``.
    """
    if isinstance(gate.get("input"), dict):
        return gate["input"]
    task_id = gate.get("task_id")
    gate_seq = gate.get("seq", 0)
    best: dict[str, Any] | None = None
    for ev in events:
        if ev.get("task_id") != task_id or not isinstance(ev.get("input"), dict):
            continue
        if ev.get("seq", 0) > gate_seq:
            continue
        if best is None or ev.get("seq", 0) >= best.get("seq", 0):
            best = ev
    return dict(best["input"]) if best else {}


def _outcome_failed(ev: dict[str, Any]) -> bool:
    """True iff an ``outcome`` event records a failure (the ``failed`` correlation flag)."""
    if "failed" in ev:
        return bool(ev["failed"])
    status = str(ev.get("status") or ev.get("result") or "").lower()
    return status in {"failed", "failure", "fail"}


def _render_case_yaml(escape: Escape) -> str:
    """Serialize an :class:`Escape` to a draft ``case.yaml`` body (proposed RED case)."""
    doc = {
        "id": escape.case_id,
        "component": escape.component,
        "input": escape.input,
        "rule_ids": [],
        "red_proof": escape.red_proof,
        "status": "proposed",
        "deterministic_checks": [
            {"name": "gate_rejects", "ref": "evals.checks:gate_rejects"},
        ],
    }
    header = (
        "# HARVESTED false-admit escape (auto-generated — status: proposed).\n"
        f"# Run {escape.red_proof.get('run_id')!r}, task {escape.task_id!r}: the gate ADMITTED\n"
        f"# (gate_result seq {escape.red_proof.get('gate_result_seq')}) but the task later FAILED\n"
        f"# (outcome seq {escape.red_proof.get('outcome_seq')}). This draft is a RED case the\n"
        "# fixed gate must REJECT. A human must ratify it (promote out of _quarantine/) before\n"
        "# it locks the suite; until then status: proposed keeps it out of green-locking.\n"
    )
    return header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
