"""Blind verification (B23) -- the verifier subprocess sees only the diff and the
repo path, never the cleaner's transcript, findings list, or rationale.

Role separation (B46) requires this to hold structurally, not by convention: even
when a caller has a transcript/findings/rationale in hand and passes them into
:func:`run_verifier` for its own bookkeeping, :func:`build_verifier_payload` is the
single construction point for the subprocess argv/env/stdin and it only ever reads
``diff`` and ``repo_path`` -- there is no code path by which the other three can reach
the subprocess. Any hunk the verifier's verdict does not affirmatively endorse is
dropped from the patch by :func:`apply_endorsed_hunks` before the cleaner applies it.

**The question is split by change class.** Blindness is only half the property; the other half is
that the judge is asked about the change it was actually given. A verifier that keeps asking "is
this deletion safe?" while af-clean grows non-deletion verbs -- consolidation, annotation, lint
fixes, JS->TS -- approves whole classes of change nobody ever checked, which is precisely the
failure the blind verifier exists to prevent. So each class carries its own question
(:data:`_CLASS_QUESTIONS`), a caller SELECTS a class rather than writing prose, and a class with no
question raises instead of falling back to the deletion one.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .findings import (
    CLASS_ANNOTATION,
    CLASS_CONSOLIDATION,
    CLASS_CODE_DELETION,
    CLASS_DELETION,
    CLASS_JS_TO_TS,
    CLASS_LINT_FIX,
    CLASS_MIGRATION,
    CLASS_REPORT_ONLY,
    CLASS_SPLIT,
)

#: Default argv for the blind verifier subprocess -- a fresh, non-interactive CLI
#: invocation (OPEN-12 resolved: fresh process, not a Task-tool subagent), so the
#: verifier cannot inherit the caller's tool context or conversation state.
# The instruction the judge needs. It is built HERE, never by a caller, so B23 still holds: nothing
# about how the diff was produced can reach the subprocess.
_PREAMBLE = (
    "You are a BLIND code-change verifier. You are shown ONLY a unified diff. You do not know "
    "who produced it or why, and you must not assume it is correct.\n\n"
)

_REPLY_FORMAT = (
    '\n\nReply with ONLY this JSON and nothing else: {"endorsed_hunk_ids": ["h1"]} to endorse the '
    'diff, or {"endorsed_hunk_ids": []} to refuse it. No prose, no code fences.'
)

# ONE QUESTION PER CHANGE CLASS.
#
# The whole safety story of af-clean rests on the verifier answering a question the cleaner did not
# write. That only holds while the question MATCHES the change. A verifier still asking "is this
# deletion safe?" about an added type annotation is not verifying anything -- it is rubber-stamping,
# and it would be the blind-verifier design defeating itself. So every widening of af-clean's verbs
# ships its own question here, and a class with no question cannot be verified at all (see
# :func:`instruction_for`).
_CLASS_QUESTIONS: dict[str, str] = {
    CLASS_DELETION: (
        "Decide whether the deletion is safe: it must remove nothing that carries information or "
        "behaviour. A comment that merely restates the identifier it annotates is safe to remove. A "
        "comment carrying a reason, an invariant, a caveat, a date, or an incident reference is NOT. "
        "Any change to executable code is NOT safe to endorse."
    ),
    CLASS_CODE_DELETION: (
        "Executable dead code has been deleted. Endorse only when the diff itself and its located "
        "reachability proof establish that every removed executable path is unreachable, every "
        "preserved caller and observable behavior is identical, no compatibility or migration "
        "obligation is being discarded, and tier-2 witnesses cover the affected public surfaces. "
        "Refuse when reachability is unknown, a dynamic/string/import caller could still exist, "
        "tests merely stop asserting removed behavior, or the diff contains an unrelated semantic "
        "change. The deletion must be complete and bounded: imports, helpers, dispatch, and tests "
        "may disappear only when they exist solely for the unreachable paths."
    ),
    CLASS_CONSOLIDATION: (
        "Two or more near-identical constructs have been merged into one. Decide whether EVERY "
        "former call site behaves identically afterwards. Look specifically for a divergence "
        "between the 'duplicates' that the merge ERASES: a different default, a different error "
        "branch, a different order of effects, a different type at one site only. If the merged "
        "form requires the callers to differ by a flag or a branch, that is a failed "
        "centralization -- refuse it. Endorse only if the behaviour at every site is unchanged."
    ),
    CLASS_SPLIT: (
        "One module has been divided into smaller modules. Decide whether this is a purely "
        "structural move whose public behaviour is identical. Every former public import path "
        "and symbol, executable or module entry point, command name, argument/default, help byte, "
        "stdout/stderr byte, exit code, validation and error order, side effect, and persistence "
        "boundary must remain available and unchanged. Refuse if the split drops or duplicates "
        "dispatch, forks policy across compatibility shims, introduces an import cycle or new "
        "observable eager import, or mixes in a semantic change. Endorse only if callers cannot "
        "observe the relocation."
    ),
    CLASS_MIGRATION: (
        "State has been migrated from one canonical representation to another. Decide whether "
        "EVERY source record maps exactly once without loss, duplication, invented provenance, "
        "identity drift, reordered evidence, or weakened validation. The source must remain "
        "reconstructable through a pinned export when byte stability is promised; malformed or "
        "ambiguous source data must be refused or explicitly dispositioned, never guessed. "
        "Crash/restart must be idempotent, and no old write path may remain a competing source of "
        "truth after cutover. Refuse mixed semantic policy changes. Endorse only a complete, "
        "witnessed, reversible cutover."
    ),
    CLASS_ANNOTATION: (
        "Type annotations have been added or tightened. Judge CORRECTNESS, not acceptance: a "
        "checker accepting an annotation does not make it true. Refuse if any added type is wider "
        "than the value it describes (`Any`, `object`, a bare `dict`/`Array`, an over-broad union) "
        "where a precise type is evident from the code, and refuse any new `# type: ignore`, "
        "`@ts-ignore`, `@ts-expect-error`, or checker-exclusion added to make the diff pass. "
        "Refuse any change to runtime behaviour: an annotation diff must be behaviour-neutral. "
        "Endorse only if every added type is both CORRECT and behaviour-neutral."
    ),
    CLASS_LINT_FIX: (
        "A linter's fix has been applied. Decide whether it is purely stylistic. Refuse if it "
        "changed semantics -- a rewritten comparison (`==` to `is`, or a reordered boolean), a "
        "changed iteration order, a removed side effect, a narrowed exception clause, an altered "
        "truthiness test. Formatting, import ordering, whitespace, and unused-import removal are "
        "safe. Endorse only if the emitted behaviour is identical."
    ),
    CLASS_JS_TO_TS: (
        "A JavaScript file has been converted to TypeScript. Decide whether the emitted behaviour "
        "is unchanged. Refuse if `any`, a non-null assertion (`!`), or a cast was introduced to "
        "make it compile rather than because it is true; refuse if the import graph changed "
        "(a moved file, a rewritten specifier, a changed default/named export shape); refuse if "
        "any statement's runtime meaning differs. Endorse only a rename plus honest types."
    ),
}


#: Classes that propose no edit, and so have no verifier question by construction. A posture report
#: is actioned by a human; there is no diff for a judge to endorse.
UNVERIFIABLE_CLASSES = frozenset({CLASS_REPORT_ONLY})


def instruction_for(change_class: str) -> str:
    """The blind verifier's full system prompt for ``change_class``.

    Raises ``ValueError`` for a class with no question -- including ``report-only``, which proposes
    no edit and therefore must never be sent to a verifier at all. Failing loudly here is the point:
    silently falling back to the deletion question is exactly the rubber-stamp this split exists to
    prevent.
    """
    question = _CLASS_QUESTIONS.get(change_class)
    if question is None:
        raise ValueError(
            f"no blind-verifier question for change class {change_class!r}; a change class without "
            "its own question cannot be verified"
        )
    return _PREAMBLE + question + _REPLY_FORMAT


_VERIFIER_INSTRUCTION = instruction_for(CLASS_DELETION)

# The verifier is a JUDGE, not an agent, and the argv is what decides which one the CLI behaves as.
# `--tools ""` gives it no tools; `--system-prompt` replaces the agentic prompt with the instruction
# above. Measured on one identical diff:
#   old argv (bare payload, default tools): 11 turns, 45s, $0.45, is_error -- and no verdict was
#     ever POSSIBLE, because nothing told the model it was verifying anything.
#   this argv:                               1 turn,   4s, $0.014, {"endorsed_hunk_ids": ["h1"]}
# --max-turns is deliberately absent: capping it produced `error_max_turns` rather than an answer
# (the cap counts the user's turn too). Removing the TOOLS is what makes it answer in one turn.
DEFAULT_ARGV: list[str] = [
    "claude", "-p", "--output-format", "json",
    "--tools", "",
    "--system-prompt", _VERIFIER_INSTRUCTION,
]


def argv_for(change_class: str) -> list[str]:
    """:data:`DEFAULT_ARGV` with the question for ``change_class`` in the system-prompt slot."""
    argv = list(DEFAULT_ARGV)
    argv[-1] = instruction_for(change_class)
    return argv


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
    change_class: str = CLASS_DELETION,
    argv: Sequence[str] | None = None,
    base_env: Mapping[str, str] | None = None,
) -> VerifierPayload:
    """Build the subprocess launch payload for the blind verifier.

    ``stdin`` and the ``AF_CLEAN_REPO_PATH`` env var are built from ``diff`` and
    ``repo_path`` alone -- this is the sole construction point for the verifier
    payload, so no caller can smuggle a transcript, findings list, or rationale into
    it (B23).

    ``change_class`` selects WHICH question the judge is asked. It is a class name, never prose:
    the caller cannot write the question, only choose from the fixed set in :data:`_CLASS_QUESTIONS`,
    so the split widens what af-clean can verify without reopening the channel B23 closes.
    """
    env = dict(base_env if base_env is not None else os.environ)
    env["AF_CLEAN_REPO_PATH"] = repo_path
    # The payload keeps its original two-key shape. The task the judge needs rides in argv via
    # --system-prompt instead, which is both where it belongs and what keeps this contract intact:
    # nothing about how the diff was produced can reach the subprocess (B23).
    stdin = json.dumps({"diff": diff, "repo_path": repo_path})
    return VerifierPayload(
        argv=list(argv if argv is not None else argv_for(change_class)),
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
    # `claude -p --output-format json` wraps the model's answer in its OWN envelope, so the verdict
    # arrives as a JSON string inside "result" rather than at the top level. Reading only the top
    # level meant the parser could never see an endorsement -- it fail-closed on every real run.
    if "endorsed_hunk_ids" not in data and isinstance(data.get("result"), str):
        inner = data["result"].strip()
        if inner.startswith("```"):
            inner = inner.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
        try:
            nested = json.loads(inner)
        except (json.JSONDecodeError, TypeError):
            return VerifierVerdict()
        if not isinstance(nested, dict):
            return VerifierVerdict()
        data = nested
    ids = data.get("endorsed_hunk_ids")
    if not isinstance(ids, list):
        return VerifierVerdict()
    return VerifierVerdict(endorsed_hunk_ids=frozenset(str(i) for i in ids))


SubprocessRunner = Callable[..., Any]


def run_verifier(
    diff: str,
    repo_path: str,
    *,
    change_class: str = CLASS_DELETION,
    transcript: Any = None,
    findings: Any = None,
    rationale: Any = None,
    argv: Sequence[str] | None = None,
    base_env: Mapping[str, str] | None = None,
    runner: SubprocessRunner,
) -> VerifierVerdict:
    """Launch the blind verifier subprocess and return its verdict.

    ``change_class`` picks the question asked of the diff (see :func:`instruction_for`); it defaults
    to ``"deletion"``, the question this verifier asked before the split.

    ``transcript``/``findings``/``rationale`` exist only so a caller sitting on that
    context can pass it through one call without a special-cased signature; none of
    the three ever reaches the subprocess -- :func:`build_verifier_payload` builds the
    argv/env/stdin from ``diff`` and ``repo_path`` alone (B23).
    """
    payload = build_verifier_payload(
        diff, repo_path, change_class=change_class, argv=argv, base_env=base_env)
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
