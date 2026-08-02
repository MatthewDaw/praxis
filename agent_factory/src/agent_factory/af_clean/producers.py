"""The default finding producers — the missing wiring between af-clean's detectors and its engine.

``entry.run_e1`` takes a ``produce_findings`` callable and deliberately does not define one: the
engine must not care where candidates come from, because E1 (a repo path) and E2 (a ticket diff)
supply them from different directions. But nothing shipped a producer either, so the human entry
point had no executable path at all — ``/af-clean`` could be typed, and then nothing could run.

This module is that path. It converts the detectors that already exist
(:mod:`agent_factory.af_clean_comment_triage`, :mod:`agent_factory.af_clean_scar_detection`) into
:class:`~agent_factory.af_clean.findings.Finding` candidates, and leaves admission to
``admit_finding`` — a producer PROPOSES, it never decides. Every candidate here is deliberately
emitted with a real ``Location``, because the admission gate drops unlocated claims and a producer
that cannot say where it is looking has not found anything.

It also carries the typing/lint POSTURE detections (:mod:`.typing_posture`), which are findings in
the same sense — located, admitted, reported — but propose no edit at all: they answer "did the
checker actually run, and does anything enforce it?", a question whose remedy is always a human
decision.

Deliberately NOT here: deletion proposals. A producer runs before blind verification, so anything
it emits at ``enforce`` tier would be a deletion nobody has corroborated yet. Comment slop is
reported at ``advise``, and scars are reported as evidence AGAINST removal, never for it.
"""

from __future__ import annotations

import io
import re
import subprocess
import tokenize
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from ..af_clean_comment_triage import classify_comment, signature_tokens
from ..af_clean_scar_detection import detect_scar
from .findings import Finding, Location
from .typing_posture import CheckerRun, typing_posture_findings

# Comment syntax per suffix. Only line comments: a block comment spans lines, and a finding that
# cannot name ONE line is not located enough to admit.
_LINE_COMMENT: dict[str, tuple[str, ...]] = {
    ".py": ("#",),
    ".rb": ("#",),
    ".sh": ("#",),
    ".ts": ("//",),
    ".tsx": ("//",),
    ".js": ("//",),
    ".jsx": ("//",),
    ".go": ("//",),
    ".rs": ("//",),
    ".java": ("//",),
    ".swift": ("//",),
    ".kt": ("//",),
    ".c": ("//",),
    ".h": ("//",),
    ".cpp": ("//",),
}

# The identifier on the line a comment annotates. Intentionally loose: the triage call only needs
# the tokens of the nearby symbol to judge information gain, not a parse tree.
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
                        ".next", "target", "vendor", ".mypy_cache", ".pytest_cache"})


def iter_source_files(scope: Path, exempt: Iterable[str] = ()) -> Iterator[Path]:
    """Source files under ``scope``, skipping obvious machine-owned trees and anything the
    exemption manifest already claimed. ``exempt`` holds repo-relative path prefixes."""
    exempt_prefixes = tuple(str(e).strip("/") for e in exempt if str(e).strip())
    for path in sorted(scope.rglob("*")):
        if not path.is_file() or path.suffix not in _LINE_COMMENT:
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if exempt_prefixes:
            try:
                rel = str(path.relative_to(scope))
            except ValueError:
                rel = str(path)
            if any(rel == p or rel.startswith(p + "/") for p in exempt_prefixes):
                continue
        yield path


_SIGNATURE = re.compile(r"^\s*(?:def|class|func|function|fn)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(?([^)]*)")


def _annotated_tokens(lines: Sequence[str], idx: int) -> frozenset[str]:
    """The identifier tokens a comment is judged against: the code it annotates PLUS the signature
    of the symbol enclosing it.

    The enclosing signature is not optional. A comment reading "increment counter" sitting inside
    ``def increment_counter(self, counter)`` restates its method exactly, but compared only against
    the next line (``counter = counter + 1``) it scores 0.5 overlap and survives as ambiguous --
    below the 0.85 near-subset bar. Judged against the signature too it scores 1.0 and is correctly
    eligible. Comments restate the SYMBOL they document far more often than the one statement
    beneath them, so omitting the signature makes the detector miss its most common case.
    """
    prefixes = tuple(p for group in _LINE_COMMENT.values() for p in group)
    tokens: set[str] = set()

    for j in range(idx + 1, min(idx + 4, len(lines))):
        stripped = lines[j].strip()
        if stripped and not stripped.startswith(prefixes):
            tokens |= set(_IDENT.findall(stripped))
            break
    else:
        same = lines[idx]
        for p in prefixes:
            if p in same:
                same = same.split(p, 1)[0]
                break
        tokens |= set(_IDENT.findall(same))

    for j in range(idx, max(idx - 40, -1), -1):
        m = _SIGNATURE.match(lines[j])
        if m:
            name, params = m.group(1), m.group(2)
            tokens |= set(signature_tokens(name, _IDENT.findall(params)))
            break

    return frozenset(tokens)


def _python_comments(text: str) -> dict[int, str] | None:
    """``{1-based line: comment text}`` for the REAL comments in Python source, via ``tokenize``.

    Line-scanning for a leading ``#`` cannot tell a comment from a ``#`` inside a string literal,
    and that is not a hypothetical: on a real run it proposed deleting the comments inside af-clean's
    OWN test fixtures — the ``SLOP = '''...'''`` constants written to look like slop so the detector
    can be tested against them. Six of eighteen findings, and acting on any of them would have
    broken the tests that prove the detector works.

    Returns ``None`` when the file will not tokenize (a syntax error, an exotic encoding), so the
    caller falls back to the line scan rather than silently reporting a file has no comments.
    """
    comments: dict[int, str] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                comments[tok.start[0]] = tok.string.lstrip("#").strip()
    except (tokenize.TokenError, SyntaxError, IndentationError, UnicodeDecodeError, ValueError):
        return None
    return comments


def comment_findings(scope: Path, *, repo_root: Path | None = None,
                     exempt: Iterable[str] = ()) -> list[Finding]:
    """Comments whose triage verdict is slop, as ``advise``-tier located findings.

    Advise, never enforce: information-gain triage is a judgment about prose, and the applier is
    witness-tiered precisely so a judgment call cannot delete on its own authority.
    """
    root = Path(repo_root) if repo_root else scope
    out: list[Finding] = []
    for path in iter_source_files(scope, exempt):
        prefixes = _LINE_COMMENT.get(path.suffix, ())
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = source.splitlines()
        # Python gets a real tokenizer; everything else keeps the line scan. The same string-literal
        # blindness exists for `//` inside a JS/TS string, but a correct fix there needs a parser per
        # language, and a half-parser that gets it wrong on template literals would be worse than a
        # heuristic that is honest about being one.
        tokenized = _python_comments(source) if path.suffix == ".py" else None

        for idx, raw in enumerate(lines):
            if tokenized is not None:
                # Own-line comments only, exactly as the line scan found them. tokenize also yields
                # TRAILING comments (`x = 1  # why`), but the applier removes whole LINES, so a
                # finding on one of those could only ever be refused — and the widening would be
                # invisible noise rather than a capability.
                text = tokenized.get(idx + 1)
                if not text or not raw.strip().startswith("#"):
                    continue
            else:
                stripped = raw.strip()
                prefix = next((p for p in prefixes if stripped.startswith(p)), None)
                if prefix is None:
                    continue
                text = stripped[len(prefix):].strip()
                if not text:
                    continue
            verdict = classify_comment(text, _annotated_tokens(lines, idx))
            # "eligible" is the ONLY removable verdict. "protected" carries a WHY marker and
            # "ambiguous" survives by default -- the triage module deliberately makes survival the
            # fallback, and a producer that widened that set would be overriding the one judgment
            # this whole module exists to make.
            if str(verdict.verdict).strip().casefold() != "eligible":
                continue
            try:
                rel = str(path.relative_to(root))
            except ValueError:
                rel = str(path)
            out.append(Finding(
                rule="comment-no-information-gain",
                tier="advise",
                location=Location(file=rel, line=idx + 1),
                pole="bloat",
                proposal=f"restates the signature; drop comment: {text[:70]}",
            ))
    return out


# ``detect_scar`` returns ``"advisory"`` for a scar and ``"eligible"`` for a clean construct -- it
# NEVER returns the string ``"scar"``. Comparing against ``"scar"`` (as this did) matched nothing,
# so this producer could not emit a single KEEP finding and the R23/B21 scar guard was inert: the
# ``is_scar`` must-not-happen refusal in ``af_clean_witness.decide`` keys on the rule name only this
# function produces, so it never fired either. Bind the constant to the detector's real vocabulary.
_SCAR_VERDICT = "advisory"


def scar_findings(repo_root: Path, candidates: Sequence[tuple[str, int]]) -> list[Finding]:
    """Defensive code with a bug-fix commit behind it, reported as a SCAR.

    These exist to STOP removals, not to propose them: a caller that has already decided to delete
    something checks here first and finds the blame evidence that the defence is load-bearing.

    A blame that CANNOT be run (not a git repo, a file git does not track, a timeout) is reported as
    a KEEP too, not skipped. This producer's whole job is to protect; "I could not check for a scar"
    is not "there is no scar", and silently dropping the candidate would hand the applier a
    construct with no protective evidence attached -- absence of evidence rendered as evidence of
    absence, on the one signal that exists to stop a deletion.
    """
    out: list[Finding] = []
    for file_path, line in candidates:
        try:
            scar = detect_scar(Path(repo_root), file_path, line)
        except Exception as exc:  # noqa: BLE001 - any blame failure => unknown => protect
            out.append(Finding(
                rule="defensive-code-is-a-scar",
                tier="advise",
                location=Location(file=file_path, line=line),
                pole="bloat",
                proposal=(f"KEEP (unverified): blame could not be run here "
                          f"({type(exc).__name__}: {exc}); scar status is UNKNOWN, so this defence "
                          f"is protected rather than cleared for removal"),
            ))
            continue
        if str(scar.verdict).strip().casefold() != _SCAR_VERDICT:
            continue
        out.append(Finding(
            rule="defensive-code-is-a-scar",
            tier="advise",
            location=Location(file=file_path, line=line),
            pole="bloat",
            proposal=(f"KEEP: {scar.construct} traced to "
                      f"{len(scar.commits)} bug-fix commit(s); removal would reopen a fixed bug"),
        ))
    return out


def git_added_paths(repo_root: Path, base_ref: str = "") -> list[str]:
    """Repo-relative paths ADDED since ``base_ref`` (default: the merge-base with the default
    branch), plus untracked files. Empty when git cannot answer — an unanswerable question is not
    evidence that nothing was added, and the only consumer of this list emits findings ABOUT the
    paths in it, so an empty list under-reports rather than mis-reports.
    """
    def _git(*args: str) -> str:
        try:
            proc = subprocess.run(["git", *args], cwd=repo_root, capture_output=True,
                                  text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return ""
        return proc.stdout if proc.returncode == 0 else ""

    base = base_ref
    if not base:
        for candidate in ("origin/main", "origin/master", "main", "master"):
            merge_base = _git("merge-base", "HEAD", candidate).strip()
            if merge_base:
                base = merge_base
                break
    added: list[str] = []
    if base:
        for line in _git("diff", "--name-status", "--diff-filter=A", base, "HEAD").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                added.append(parts[-1].strip())
    added.extend(p.strip() for p in _git("ls-files", "--others", "--exclude-standard").splitlines())
    return [p for p in dict.fromkeys(added) if p]


def default_producer(*, exempt: Iterable[str] = (), repo_root: Path | None = None,
                     checker_runs: Sequence[CheckerRun] = ()):
    """The repo-scoped producer ``run_e1`` expects: ``(scope: Path) -> Sequence[Finding]``.

    Posture findings are keyed off ``repo_root``, not the scope: a checker's configuration and its
    enforcement live at the repo root even when the caller scoped the run to one subtree, and a
    subtree cannot answer "is this gate enforced?" about the repo that contains it.
    """
    exempt = tuple(exempt)
    checker_runs = tuple(checker_runs)

    def produce(scope: Path) -> Sequence[Finding]:
        scope = Path(scope)
        root = Path(repo_root) if repo_root is not None else scope
        return [
            *comment_findings(scope, repo_root=root, exempt=exempt),
            *typing_posture_findings(
                root,
                checker_runs=checker_runs,
                census_file_count=sum(1 for _ in iter_source_files(scope, exempt)),
                added_paths=git_added_paths(root),
                exempt=exempt,
            ),
        ]

    return produce
