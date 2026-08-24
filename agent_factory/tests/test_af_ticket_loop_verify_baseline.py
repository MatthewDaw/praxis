"""Post-merge verification must attribute a failure before it regresses a ticket.

Observed live, praxis round #1 (2026-08-24):

    verdict=fail gates_green=False regressed=1
    "although R0a's pinned acceptance itself passes 4/4 and its diff does not touch the failing
     frontend/skill files, the round rule requires regressing the whole batch when a red merged
     tree cannot be attributed to another ticket"

The verifier was right about the rule and the rule was wrong. It said:

    "If any gate is red, identify which ticket's change caused it and regress that ticket per
     step 3; if you genuinely cannot attribute it, regress the whole batch rather than passing a
     red tree."

with no notion of a BASELINE. This repository carries dozens of pre-existing failures — 24 in
agent_factory alone, plus a root-suite failure and ten frontend ones — so every ticket of every
round is regressed, rebuilt, and regressed again by the identical verdict. An infinite loop at full
cost, and it had already started its second lap when it was caught.

The fix gives verification a reference tree: the commit the round merged INTO, captured before a
single branch lands. A failure that reproduces there is debt, reported and not charged to anyone;
only a failure that does NOT reproduce there belongs to this round.

These tests RENDER the prompt through bash, the way the driver does. Asserting on the source would
miss the failure mode that matters most here — a baseline that does not interpolate leaves the
verifier reading instructions about a commit named `$AF_PREMERGE_SHA`, and it will fall back to
regressing whatever looks red.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "af-ticket-loop.sh"


def _source() -> str:
    return SCRIPT.read_text()


def _render(sha: str = "abc1234def", rnd: str = "7") -> str:
    """The verifier prompt as the driver actually builds it."""
    line = next(
        source_line
        for source_line in _source().splitlines()
        if source_line.strip().startswith("local vprompt=")
    )
    body = line.strip()[len("local vprompt=") :]
    prog = (
        "set -u\n"
        f'rnd={rnd}; premerge={sha}; PROJECT=praxis; ids_csv=R0a\n'
        'FINDINGS=/tmp/findings.json; VERDICT=/tmp/verdict.json\n'
        f"vprompt={body}\n"
        'printf %s "$vprompt"\n'
    )
    res = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    return res.stdout


# ------------------------------------------------------------------------------ the regression --

def test_the_unconditional_regress_the_whole_batch_rule_is_gone():
    """THE REGRESSION. This exact sentence sent R0a round the loop."""
    prompt = _render()
    assert (
        "If any gate is red, identify which ticket's change caused it and regress that ticket "
        "per step 3; if you genuinely cannot attribute it, regress the whole batch rather than "
        "passing a red tree."
    ) not in prompt


def test_the_verifier_is_given_the_commit_the_round_merged_into():
    prompt = _render(sha="deadbee1234")
    assert "deadbee1234" in prompt, "the baseline must be interpolated, not left as a variable name"
    assert "AF_PREMERGE_SHA" not in prompt, "an un-expanded variable name reads as a commit that isn't"


def test_a_failure_that_predates_the_round_may_not_regress_a_ticket():
    prompt = _render()
    lowered = prompt.lower()
    assert "pre-existing" in lowered
    assert "do not put any ticket in regressed" in lowered
    # And the WHY, so a verifier reasoning about an edge case reaches the same answer.
    assert "forever" in lowered


def test_a_new_failure_still_regresses_the_batch_when_unattributable():
    """The fix must not become a licence to pass a tree this round genuinely broke."""
    prompt = _render()
    assert "regress the whole batch rather than passing a tree this round broke" in prompt


def test_the_verdict_contract_has_somewhere_to_put_the_pre_existing_debt():
    """Without a home in the schema, pre-existing failures become invisible rather than attributed
    correctly — trading a false regression for silence, which is not an improvement."""
    prompt = _render()
    assert "preexisting which is an array of strings" in prompt
    assert "never as a shortcut for not having checked" in prompt


# --------------------------------------------------------------------- the baseline is capturable --

def test_the_baseline_is_captured_before_the_round_merges_anything():
    """Order is the whole fix: after integrate_round the pre-merge tree is unrecoverable."""
    src = _source()
    capture = src.index('AF_PREMERGE_SHA=$(git -C "$WT" rev-parse HEAD')
    integrate = src.index("\n  integrate_round\n")
    assert capture < integrate, "the baseline must be read BEFORE any branch lands"


def test_the_baseline_variable_is_safe_under_set_u():
    """`set -u` plus an unset expansion kills the whole loop silently — the failure mode this
    driver has been bitten by before."""
    src = _source()
    assert 'AF_PREMERGE_SHA=""' in src, "declare it, so an expansion before the first round is safe"
    assert 'local premerge="${AF_PREMERGE_SHA:-}"' in src


def test_verify_round_falls_back_to_a_real_commit_rather_than_an_empty_string(tmp_path: Path):
    """An empty baseline would render 'fails at ' and the rule would evaporate."""
    src = _source()
    start = src.index("verify_round(){")
    head = src[start : start + 900]
    assert 'premerge=$(git -C "$WT" rev-parse HEAD' in head

    # And prove the fallback yields something git accepts as a commit.
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["bash", "-c", "git init -q && git config user.email t@t && "
                    "git config user.name t && echo x > a && git add -A && git commit -qm base"],
                   cwd=repo, check=True, capture_output=True)
    out = subprocess.run(
        ["bash", "-c", f'set -u; WT={repo}; AF_PREMERGE_SHA=""\n'
                       'premerge="${AF_PREMERGE_SHA:-}"\n'
                       '[ -n "$premerge" ] || premerge=$(git -C "$WT" rev-parse HEAD 2>/dev/null || echo "HEAD")\n'
                       'printf %s "$premerge"'],
        capture_output=True, text=True, check=True)
    assert len(out.stdout.strip()) == 40, out.stdout


def test_gates_green_is_redefined_as_no_new_failures():
    """Leaving gates_green meaning 'the tree is entirely green' would keep every round incoherent
    on a repo that is never entirely green, and the coherence gate downgrades those to UNVERIFIED."""
    prompt = _render()
    assert "gates_green reports whether the round introduced NO NEW failures" in prompt
