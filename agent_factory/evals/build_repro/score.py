"""Scoring for the build-reproduction eval: how close did the agent's build land to the golden commit?

Three stack-free signals + one optional behavioral one (run by run_eval):
  - per-ASPECT diff-closeness via an injected LLM judge (the acceptance aspects of the bound check),
  - file-coverage overlap (agent-touched files vs golden-touched files),
  - Praxis ticket state (did the loop reach build_state="finished" + record its check pass).

The model is injected as ``Complete = (prompt) -> text`` (reuse evals.plan_repro.claude_cli) so this is
testable offline and runs on the subscription with no API key.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

Complete = Callable[[str], str]

# The acceptance aspects of the auth-password-reset-e2e check — what "done" means for R73.
ASPECTS = [
    ("request_accepted", "The player UI 'forgot password' request is implemented and accepted."),
    ("link_obtainable", "The reset link/token is obtainable via a dev mechanism (dev email transport "
        "logs/exposes the link rather than silently dropping it)."),
    ("reset_screen", "A real /reset-password?token= screen exists in the PLAYER app and renders "
        "(a redirect to /welcome would be a FAIL)."),
    ("set_new_password", "Submitting a new conforming password succeeds and the reset token is single-use "
        "(reusing it fails)."),
    ("login_new_password", "The user can then log in with the new password and the old one is rejected."),
]


def git_diff(repo: str, a: str, b: str | None = None, paths: list[str] | None = None) -> str:
    """Unified diff a..b (or a..worktree if b is None) limited to ``paths`` if given."""
    cmd = ["git", "-C", repo, "diff", f"{a}..{b}" if b else a]
    if paths:
        cmd += ["--", *paths]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8").stdout


def touched_files(repo: str, a: str, b: str | None = None) -> set[str]:
    cmd = ["git", "-C", repo, "diff", "--name-only", f"{a}..{b}" if b else a]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8").stdout
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def file_coverage(agent_files: set[str], golden_files: set[str]) -> dict:
    if not golden_files:
        return {"overlap": 0, "golden": 0, "recall": 0.0, "missed": []}
    hit = agent_files & golden_files
    return {
        "overlap": len(hit), "golden": len(golden_files),
        "recall": round(len(hit) / len(golden_files), 3),
        "missed": sorted(golden_files - agent_files),
    }


_JUDGE_SCHEMA_HINT = (
    'Respond with JSON only: {"aspects":[{"key":"<aspect key>","covered":true|false,'
    '"confidence":0.0-1.0,"evidence":"<short>"}],"overall":0.0-1.0,"notes":"<short>"}'
)


def build_judge_prompt(golden_diff: str, agent_diff: str) -> str:
    aspects = "\n".join(f"  - {k}: {d}" for k, d in ASPECTS)
    return (
        "You are scoring whether a CANDIDATE code change reproduces the BEHAVIOR of a TARGET (golden) "
        "code change for a password-reset feature. They need NOT match byte-for-byte — judge whether the "
        "candidate achieves the same observable behavior for each acceptance ASPECT below. Mark an aspect "
        "covered only if the candidate diff shows concrete evidence of it.\n\n"
        f"ACCEPTANCE ASPECTS:\n{aspects}\n\n"
        "=== TARGET (golden) diff ===\n" + golden_diff[:24000] + "\n\n"
        "=== CANDIDATE (agent) diff ===\n" + agent_diff[:24000] + "\n\n"
        + _JUDGE_SCHEMA_HINT
    )


def _loads(text: str):
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    try:
        return json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s, re.S)
        return json.loads(m.group(0)) if m else {}


@dataclass
class BuildScore:
    aspects: dict = field(default_factory=dict)     # key -> {covered, confidence, evidence}
    aspect_score: float = 0.0                        # fraction of aspects covered
    overall: float = 0.0                             # judge's holistic 0-1
    file_coverage: dict = field(default_factory=dict)
    praxis: dict = field(default_factory=dict)       # build_state, check_passed
    behavioral: dict | None = None                   # set by run_eval if --run-tests
    passed: bool = False

    def format(self) -> str:
        lines = ["BUILD-REPRO SCORE", "  aspects:"]
        for k, _ in ASPECTS:
            a = self.aspects.get(k, {})
            mark = "✓" if a.get("covered") else "✗"
            lines.append(f"    {mark} {k}: {a.get('evidence','')[:80]}")
        lines.append(f"  aspect_score : {self.aspect_score:.2f}  overall(judge): {self.overall:.2f}")
        fc = self.file_coverage
        lines.append(f"  file_coverage: {fc.get('overlap')}/{fc.get('golden')} golden files "
                     f"(recall {fc.get('recall')}); missed e.g. {fc.get('missed', [])[:5]}")
        lines.append(f"  praxis       : build_state={self.praxis.get('build_state')} "
                     f"check_passed={self.praxis.get('check_passed')}")
        if self.behavioral is not None:
            lines.append(f"  behavioral   : tests_passed={self.behavioral.get('passed')} "
                         f"({self.behavioral.get('detail','')[:80]})")
        lines.append(f"  => PASSED: {self.passed}")
        return "\n".join(lines)


def score_build(complete: Complete, golden_diff: str, agent_diff: str,
                agent_files: set[str], golden_files: set[str], praxis_state: dict,
                *, threshold: float = 0.8) -> BuildScore:
    verdict = _loads(complete(build_judge_prompt(golden_diff, agent_diff)))
    aspects = {a.get("key"): a for a in (verdict.get("aspects") or []) if a.get("key")}
    covered = sum(1 for k, _ in ASPECTS if aspects.get(k, {}).get("covered"))
    aspect_score = round(covered / len(ASPECTS), 3)
    fc = file_coverage(agent_files, golden_files)
    finished = str(praxis_state.get("build_state")) == "finished"
    bs = BuildScore(
        aspects=aspects, aspect_score=aspect_score,
        overall=float(verdict.get("overall", aspect_score)),
        file_coverage=fc, praxis=praxis_state,
    )
    # "gets close": the behavior aspects are (mostly) reproduced AND the loop drove the hard enum to
    # finished. Behavioral (green acceptance test), when run, overrides as authoritative in run_eval.
    bs.passed = (aspect_score >= threshold) and finished
    return bs
