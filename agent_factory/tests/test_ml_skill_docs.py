"""The ML skill documents carry their required structure (check plan-3aa6ccaa4778,
extended for R20's standalone-parity/registry-integration acceptance).

af-ml-model/SKILL.md must name its harness-instance section, state its stop
conditions, keep its ``## Never`` list free of any line forbidding a supervisor
driver (af-ml-model-remote IS a supervisor driver, launching the same loop
unattended via a detached tmux session -- a Never rule that forbade one would
directly contradict a sibling skill this repo already ships), and carry a named
entry quoting program.md's NEVER STOP rule by name -- so a skill rewrite cannot
land as prose that asserts parity with program.md's contract without ever stating
it structurally.

R20 additionally requires the skill to document that a standalone run registers
through the SAME registry write path and verdict rules a ticketed run uses (no
ledger row without a registered idea), that it seeds its starting ideas
interactively as ``origin=seeded`` and thereafter registers self-generated ideas
as ``origin=discovered`` under the write path's ``max_discovered_ideas`` refusal,
and that the ``## Never`` list itself records the per-trial dispatch driver and
the research-ticket bounded stop as deliberate forks from program.md's NEVER STOP
rule -- not silent contradictions of it.
"""

from __future__ import annotations

import re
from pathlib import Path

SKILL_PATH = Path(__file__).resolve().parents[2] / "agent_factory" / "skills" / "af-ml-model" / "SKILL.md"

_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)
_SUPERVISOR_FORBID_RE = re.compile(r"never.*\bsupervisor\b|\bsupervisor\b.*never", re.IGNORECASE)


def _headings(text: str) -> list[str]:
    return [h.strip() for h in _HEADING_RE.findall(text)]


def _never_list_lines(text: str) -> list[str]:
    match = re.search(r"^## Never\s*$(.*)", text, re.MULTILINE | re.DOTALL)
    assert match, "SKILL.md has no '## Never' section"
    body = match.group(1)
    next_heading = re.search(r"^#{1,6}\s", body, re.MULTILINE)
    if next_heading:
        body = body[: next_heading.start()]
    return [line for line in body.splitlines() if line.strip().startswith("-")]


def test_skill_names_a_harness_instance_section() -> None:
    text = SKILL_PATH.read_text()
    assert any("harness instance" in h.casefold() for h in _headings(text)), (
        "af-ml-model/SKILL.md must name a 'harness instance' section"
    )


def test_skill_states_its_stop_conditions() -> None:
    text = SKILL_PATH.read_text()
    assert any("stop condition" in h.casefold() for h in _headings(text)), (
        "af-ml-model/SKILL.md must name a 'stop conditions' section"
    )


def test_never_list_has_no_line_forbidding_a_supervisor_driver() -> None:
    text = SKILL_PATH.read_text()
    for line in _never_list_lines(text):
        assert not _SUPERVISOR_FORBID_RE.search(line), (
            f"'## Never' line forbids a supervisor driver, contradicting af-ml-model-remote: {line!r}"
        )


def test_skill_has_a_named_entry_quoting_the_never_stop_rule() -> None:
    text = SKILL_PATH.read_text()
    assert re.search(r"\*\*NEVER STOP\*\*.*program\.md|program\.md.*\*\*NEVER STOP\*\*", text), (
        "af-ml-model/SKILL.md must carry a named entry quoting program.md's NEVER STOP rule"
    )


def test_skill_documents_the_shared_registry_write_path_and_verdict_rules() -> None:
    text = SKILL_PATH.read_text()
    assert "write_path" in text, (
        "af-ml-model/SKILL.md must name the registry write path (knowledge/ml_registry/write_path.py)"
    )
    assert "adjudicate_verdict" in text, (
        "af-ml-model/SKILL.md must name the shared verdict rule (verdict.adjudicate_verdict) a "
        "standalone run is judged by, identical to a ticketed run"
    )
    assert "supervisor.dispatch_trial" in text, (
        "af-ml-model/SKILL.md must name the ticketed run's call (supervisor.dispatch_trial) a "
        "standalone run's own registration calls are stated to mirror"
    )


def test_skill_documents_seeded_and_discovered_idea_origins_with_the_budget_refusal() -> None:
    text = SKILL_PATH.read_text()
    assert "origin=seeded" in text, (
        "af-ml-model/SKILL.md must document a standalone run seeding its starting ideas "
        "interactively as origin=seeded"
    )
    assert "origin=discovered" in text, (
        "af-ml-model/SKILL.md must document a standalone run registering self-generated ideas "
        "as origin=discovered"
    )
    assert "max_discovered_ideas" in text, (
        "af-ml-model/SKILL.md must document the write path refusing a discovered idea past "
        "max_discovered_ideas"
    )


def test_never_list_records_the_dispatch_driver_and_bounded_stop_as_deliberate_forks() -> None:
    text = SKILL_PATH.read_text()
    fork_lines = [
        line for line in _never_list_lines(text)
        if "NEVER STOP" in line and re.search(r"fork|departure", line, re.IGNORECASE)
    ]
    assert fork_lines, (
        "af-ml-model/SKILL.md's '## Never' list must carry an entry quoting program.md's NEVER "
        "STOP rule and recording the per-trial dispatch driver + bounded stop as deliberate forks"
    )
