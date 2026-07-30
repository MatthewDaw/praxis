"""B23 blind verification: the verifier subprocess payload is narrowed to just the
diff and the repo path, and any hunk it does not affirmatively endorse is dropped.
"""

from __future__ import annotations

import json

from agent_factory.af_clean.verifier import (
    Hunk,
    VerifierVerdict,
    apply_endorsed_hunks,
    run_verifier,
)


class _FakeCompleted:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.returncode = 0


def test_verifier_subprocess_payload_excludes_transcript_findings_rationale() -> None:
    captured: dict = {}

    def fake_runner(argv, *, input, env, cwd, capture_output, text):  # noqa: A002
        captured["argv"] = argv
        captured["stdin"] = input
        captured["env"] = env
        captured["cwd"] = cwd
        return _FakeCompleted(stdout='{"endorsed_hunk_ids": []}')

    run_verifier(
        "diff --git a/x b/x\n+added line",
        "/repo/path",
        transcript=["the caller's private chain of thought"],
        findings=["finding: unused import at x.py:12"],
        rationale="I believe this hunk is safe because...",
        runner=fake_runner,
    )

    stdin_text = captured["stdin"]
    assert "the caller's private chain of thought" not in stdin_text
    assert "finding: unused import" not in stdin_text
    assert "I believe this hunk is safe" not in stdin_text

    stdin_payload = json.loads(stdin_text)
    assert set(stdin_payload.keys()) == {"diff", "repo_path"}
    assert stdin_payload["repo_path"] == "/repo/path"
    assert stdin_payload["diff"] == "diff --git a/x b/x\n+added line"

    env_text = json.dumps(captured["env"])
    assert "the caller's private chain of thought" not in env_text
    assert "finding: unused import" not in env_text
    assert "I believe this hunk is safe" not in env_text

    assert captured["cwd"] == "/repo/path"


def test_hunk_not_affirmatively_endorsed_is_dropped_from_patch() -> None:
    hunks = [Hunk(id="h1", diff_text="+a"), Hunk(id="h2", diff_text="+b")]
    verdict = VerifierVerdict(endorsed_hunk_ids=frozenset({"h1"}))

    kept = apply_endorsed_hunks(hunks, verdict)

    assert [h.id for h in kept] == ["h1"]


def test_run_verifier_end_to_end_drops_unendorsed_hunks() -> None:
    def fake_runner(argv, *, input, env, cwd, capture_output, text):  # noqa: A002
        return _FakeCompleted(stdout='{"endorsed_hunk_ids": ["h1"]}')

    hunks = [Hunk(id="h1", diff_text="+a"), Hunk(id="h2", diff_text="+b")]

    verdict = run_verifier("some diff", "/repo/path", runner=fake_runner)
    kept = apply_endorsed_hunks(hunks, verdict)

    assert [h.id for h in kept] == ["h1"]


def test_verifier_verdict_defaults_to_no_endorsement_on_malformed_output() -> None:
    def fake_runner(argv, *, input, env, cwd, capture_output, text):  # noqa: A002
        return _FakeCompleted(stdout="not json at all")

    verdict = run_verifier("some diff", "/repo/path", runner=fake_runner)

    assert verdict.endorsed_hunk_ids == frozenset()
