"""Pins the af-intake-plan write-path/bless-path acceptance condition (S9).

The MCP transport (`knowledge/mcp/server.py::praxis_add_insights`) already accepts
BOTH `space` and `snapshot` on the bulk insight write (see commit 932c7b9), so a
fresh full intake can admit its whole batch of requirement facts straight into the
target plan snapshot in one round-trip, leaving working memory untouched. The
af-intake-plan SKILL.md is the "consumer" of that transport — the doc an agent
actually follows while intaking a plan — and it must instruct that write path
instead of a stale caveat claiming the bulk call cannot target a snapshot.

This also pins the companion bless-path invariant: a plan that was authored
directly into `prd-<project>` must never be re-saved via `save_snapshot`, which
would overwrite the snapshot with (mostly unrelated) working memory.
"""
import re
from pathlib import Path

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "af-intake-plan"
    / "SKILL.md"
)


def _section(text: str, start_heading: str, end_heading: str) -> str:
    start = text.index(start_heading)
    end = text.index(end_heading, start)
    return text[start:end]


def _skill_text() -> str:
    return SKILL_PATH.read_text()


def test_write_path_step_documents_bulk_insight_write_with_space_and_snapshot():
    """Step 2 must tell the agent a fresh intake writes with `praxis_add_insights`
    (the bulk call) passing BOTH `space` and `snapshot`, so the whole batch lands
    directly in the target plan snapshot and working memory's fact count never
    moves."""
    text = _skill_text()
    step2 = _section(text, "## Step 2", "## Step 3")

    assert "praxis_add_insights(" in step2, (
        "Step 2 no longer mentions the bulk `praxis_add_insights` call — a fresh "
        "intake should admit its batch in one bulk round-trip now that the "
        "transport accepts a snapshot target."
    )

    # The bulk call block must itself carry both space= and snapshot= kwargs
    # (not just praxis_add_insight, the singular per-item sibling).
    bulk_call_match = re.search(
        r"praxis_add_insights\(([\s\S]*?)\n>?\s*\)", step2
    )
    assert bulk_call_match is not None, (
        "Could not find a `praxis_add_insights(...)` call block in Step 2 to "
        "verify it targets a snapshot."
    )
    bulk_call_body = bulk_call_match.group(1)
    assert "space" in bulk_call_body and "snapshot" in bulk_call_body, (
        "The documented `praxis_add_insights` call in Step 2 must pass both "
        "`space` and `snapshot` so the batch writes into the target plan "
        "snapshot instead of working memory."
    )


def test_write_path_step_no_longer_claims_bulk_lacks_space_snapshot_support():
    """The stale 'Bulk caveat' claiming `praxis_add_insights` exposes no
    space/snapshot parameter must be gone now that the transport supports it —
    leaving it in place would misdirect every future intake onto the slower,
    already-fixed per-item workaround."""
    text = _skill_text()
    step2 = _section(text, "## Step 2", "## Step 3")

    assert "no `space`/`snapshot`" not in step2 and "no space/snapshot" not in step2, (
        "Step 2 still claims `praxis_add_insights` exposes no space/snapshot "
        "parameter — that transport gap was closed; the doc must not tell "
        "agents to avoid the bulk write path for this reason."
    )
    assert "MUST NOT be used to admit a plan" not in step2, (
        "Step 2 still forbids using the bulk `praxis_add_insights` call to "
        "admit a plan — that prohibition is stale now that it accepts a "
        "snapshot target."
    )


def test_bless_path_never_calls_save_snapshot_on_a_directly_authored_plan():
    """B9's default bless path (a plan already authored into prd-<project>) must
    never call save_snapshot — doing so overwrites the snapshot with working
    memory and destroys the plan just authored."""
    text = _skill_text()
    bless_section = _section(
        text,
        "### Blessing a plan that already lives in the snapshot",
        "## C0",
    )

    default_path = _section(
        bless_section,
        "### Blessing a plan that already lives in the snapshot",
        "> **Legacy path",
    )

    assert "DO NOT CALL `save_snapshot`" in default_path, (
        "The default (direct-to-snapshot) bless path must explicitly forbid "
        "calling save_snapshot."
    )
    # No bare, unqualified `save_snapshot(` call sits in the default bless path
    # itself (the only save_snapshot mention allowed there is the prohibition).
    calls_in_default_path = re.findall(r"save_snapshot\(", default_path)
    assert len(calls_in_default_path) == 0, (
        "The default bless path contains an actual `save_snapshot(...)` call "
        "— on the direct-to-snapshot path this overwrites prd-<project> with "
        "working memory and destroys the plan."
    )
