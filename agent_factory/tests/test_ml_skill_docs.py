"""The ML skill documents carry their required structure (check plan-3aa6ccaa4778).

af-ml-model/SKILL.md must name its harness-instance section, state its stop
conditions, keep its ``## Never`` list free of any line forbidding a supervisor
driver (af-ml-model-remote IS a supervisor driver, launching the same loop
unattended via a detached tmux session -- a Never rule that forbade one would
directly contradict a sibling skill this repo already ships), and carry a named
entry quoting program.md's NEVER STOP rule by name -- so a skill rewrite cannot
land as prose that asserts parity with program.md's contract without ever stating
it structurally.
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
