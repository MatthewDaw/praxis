"""Blind verification (B23) -- the verifier subprocess sees only the diff and the
repo path, never the cleaner's transcript, findings list, or rationale.

Role separation (B46) requires this to hold structurally, not by convention: even
when a caller has a transcript/findings/rationale in hand and passes them into
:func:`run_verifier` for its own bookkeeping, :func:`build_verifier_payload` is the
single construction point for the subprocess argv/env/stdin and it only ever reads
``diff`` and ``repo_path`` -- there is no code path by which the other three can reach
the subprocess. Any hunk the verifier's verdict does not affirmatively endorse is
dropped from the patch by :func:`apply_endorsed_hunks` before the cleaner applies it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: Default argv for the blind verifier subprocess -- a fresh, non-interactive CLI
#: invocation (OPEN-12 resolved: fresh process, not a Task-tool subagent), so the
#: verifier cannot inherit the caller's tool context or conversation state.
DEFAULT_ARGV: list[str] = ["claude", "-p", "--output-format", "json"]


@dataclass(frozen=True)
class Hunk:
    """One reviewable unit of a patch: a stable id plus its diff text."""

    id: str
    diff_text: str


@dataclass(frozen=True)
class VerifierPayload:
    """The exact argv/env/stdin the verifier subprocess is launched with."""

    argv: list[str]
    env: dict[str, str]
    stdin: str

    def stdin_payload(self) -> dict[str, Any]:
        return json.loads(self.stdin)


def build_verifier_payload(
    diff: str,
    repo_path: str,
    *,
    argv: Sequence[str] | None = None,
    base_env: Mapping[str, str] | None = None,
) -> VerifierPayload:
    """Build the subprocess launch payload for the blind verifier.

    ``stdin`` and the ``AF_CLEAN_REPO_PATH`` env var are built from ``diff`` and
    ``repo_path`` alone -- this is the sole construction point for the verifier
    payload, so no caller can smuggle a transcript, findings list, or rationale into
    it (B23).
    """
    env = dict(base_env if base_env is not None else os.environ)
    env["AF_CLEAN_REPO_PATH"] = repo_path
    stdin = json.dumps({"diff": diff, "repo_path": repo_path})
    return VerifierPayload(
        argv=list(argv if argv is not None else DEFAULT_ARGV),
        env=env,
        stdin=stdin,
    )


@dataclass(frozen=True)
class VerifierVerdict:
    """Which hunks the verifier affirmatively endorsed. Absent == not endorsed."""

    endorsed_hunk_ids: frozenset[str] = field(default_factory=frozenset)


def parse_verifier_output(raw_stdout: str) -> VerifierVerdict:
    """Parse the verifier's JSON verdict. Malformed/missing output endorses nothing.

    Endorsement must be affirmative: any parse failure or unexpected shape yields an
    empty verdict rather than raising, so a broken verifier subprocess can never
    smuggle an unreviewed hunk through by accident.
    """
    try:
        data = json.loads(raw_stdout)
    except (json.JSONDecodeError, TypeError):
        return VerifierVerdict()
    if not isinstance(data, dict):
        return VerifierVerdict()
    ids = data.get("endorsed_hunk_ids")
    if not isinstance(ids, list):
        return VerifierVerdict()
    return VerifierVerdict(endorsed_hunk_ids=frozenset(str(i) for i in ids))


SubprocessRunner = Callable[..., Any]


def run_verifier(
    diff: str,
    repo_path: str,
    *,
    transcript: Any = None,
    findings: Any = None,
    rationale: Any = None,
    argv: Sequence[str] | None = None,
    base_env: Mapping[str, str] | None = None,
    runner: SubprocessRunner,
) -> VerifierVerdict:
    """Launch the blind verifier subprocess and return its verdict.

    ``transcript``/``findings``/``rationale`` exist only so a caller sitting on that
    context can pass it through one call without a special-cased signature; none of
    the three ever reaches the subprocess -- :func:`build_verifier_payload` builds the
    argv/env/stdin from ``diff`` and ``repo_path`` alone (B23).
    """
    payload = build_verifier_payload(diff, repo_path, argv=argv, base_env=base_env)
    result = runner(
        payload.argv,
        input=payload.stdin,
        env=payload.env,
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    stdout = getattr(result, "stdout", "")
    return parse_verifier_output(stdout)


def apply_endorsed_hunks(hunks: Sequence[Hunk], verdict: VerifierVerdict) -> list[Hunk]:
    """Drop any hunk the verifier did not affirmatively endorse from the patch."""
    return [h for h in hunks if h.id in verdict.endorsed_hunk_ids]
