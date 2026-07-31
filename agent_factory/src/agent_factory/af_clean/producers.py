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

Deliberately NOT here: deletion proposals. A producer runs before blind verification, so anything
it emits at ``enforce`` tier would be a deletion nobody has corroborated yet. Comment slop is
reported at ``advise``, and scars are reported as evidence AGAINST removal, never for it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from ..af_clean_comment_triage import classify_comment, signature_tokens
from ..af_clean_scar_detection import detect_scar
from .findings import Finding, Location

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
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for idx, raw in enumerate(lines):
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


def scar_findings(repo_root: Path, candidates: Sequence[tuple[str, int]]) -> list[Finding]:
    """Defensive code with a bug-fix commit behind it, reported as a SCAR.

    These exist to STOP removals, not to propose them: a caller that has already decided to delete
    something checks here first and finds the blame evidence that the defence is load-bearing.
    """
    out: list[Finding] = []
    for file_path, line in candidates:
        try:
            scar = detect_scar(Path(repo_root), file_path, line)
        except Exception:
            continue
        if str(scar.verdict).strip().casefold() != "scar":
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


def default_producer(*, exempt: Iterable[str] = ()):
    """The repo-scoped producer ``run_e1`` expects: ``(scope: Path) -> Sequence[Finding]``."""
    exempt = tuple(exempt)

    def produce(scope: Path) -> Sequence[Finding]:
        return comment_findings(Path(scope), exempt=exempt)

    return produce
