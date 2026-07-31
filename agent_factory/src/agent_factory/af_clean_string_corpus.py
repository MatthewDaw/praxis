"""B6/B20/S3 — the af-clean string-dispatch corpus and whole-token quarantine match.

B6 enumerates every string literal in the target repo (a Python AST walk today; other
literal-bearing surfaces are future work) so that a symbol referenced only by name from a
string — a command string in ``hooks.json``, an interpolated path, a registry key — is never
proposed for deletion just because static import analysis finds no importer.

B20 quarantines on a **whole-token, dispatch-context** match: a corpus entry only protects a
symbol when (a) the symbol appears as a whole token after splitting the literal on path
separators, dots, and whitespace, and (b) the literal was used in a plausible dispatch
context (a path/command-shaped string, or an argument to something other than a
logging/print call). A token that only ever shows up inside a log message is explicitly
*not* a dispatch context, so short generic names (``run``, ``main``, ``get``) do not become
permanently unpurgeable.

S3 redacts secret-shaped values (API keys, tokens, connection strings, private keys) before
they are ever quoted into the corpus, a report line, or a ledger entry, and excludes
secret-bearing files (``.env`` and variants) from corpus enumeration entirely.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

#: Splits a string literal into dispatch tokens: path separators, dots, whitespace.
#: ``.../hooks/build_completeness_gate.py`` -> {"hooks", "build_completeness_gate", "py", ...}
_TOKEN_SPLIT_RE = re.compile(r"[\\/.\s]+")

#: Call names whose string arguments are log/print messages, never dispatch context.
_LOG_CALL_NAMES = frozenset(
    {"log", "logger", "logging", "print", "debug", "info", "warn", "warning", "error", "critical", "exception"}
)

#: Common secret shapes (S3). Matched values are redacted before storage anywhere.
_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(?:sk|pk|rk)-[a-zA-Z0-9]{16,}\b"),  # sk-... style API keys
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"(?i)\bapi[_-]?key\b\s*[:=]\s*['\"]?[A-Za-z0-9/+=_\-]{12,}['\"]?"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-_.]{16,}"),
    re.compile(r"(?i)[a-z][a-z0-9+.\-]*://[^:\s]+:[^@\s]+@[^\s'\"]+"),  # user:pass@host connection string
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]

_REDACTED = "[REDACTED]"


@dataclass(frozen=True)
class CorpusEntry:
    """One B6 corpus entry: a string literal, its dispatch tokens, and its context."""

    raw: str
    tokens: frozenset[str]
    context: str  # "dispatch" | "log" | "prose"
    file: str
    line: int


def is_secret_bearing_file(path: Path) -> bool:
    """S3: ``.env`` and its variants (``.env.local`` etc.) are excluded from enumeration."""
    return path.name == ".env" or path.name.startswith(".env.")


def looks_like_secret(text: str) -> bool:
    """True if ``text`` matches a common secret shape (API key, token, connection string)."""
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def redact(text: str) -> str:
    """Replace every secret-shaped substring of ``text`` with a redaction marker (S3)."""
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted


def _tokenize(raw: str) -> frozenset[str]:
    return frozenset(token for token in _TOKEN_SPLIT_RE.split(raw) if token)


def _call_func_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _classify_context(raw: str, enclosing_call_name: str | None) -> str:
    if enclosing_call_name and enclosing_call_name.lower() in _LOG_CALL_NAMES:
        return "log"
    if "/" in raw or "\\" in raw or re.search(r"\.[A-Za-z0-9_]{1,8}$", raw):
        return "dispatch"
    return "prose"


@dataclass(frozen=True)
class CorpusScan:
    """A corpus build plus the files it could NOT read/parse.

    The gap is first-class because the corpus answers a protective question -- "is this symbol
    referenced from a string?" -- and a file that failed to parse contributes no literals. Without
    ``unscanned``, "not in the corpus" from a partial scan is indistinguishable from "not
    string-referenced anywhere", which turns an unreadable file into a licence to delete.
    """

    entries: list[CorpusEntry]
    unscanned: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.unscanned


def build_corpus_scan(root: Path) -> CorpusScan:
    """Enumerate the B6 string-dispatch corpus, RECORDING every file that could not be scanned.

    Prefer this over :func:`build_corpus` anywhere the result gates a removal: only this form can
    tell a caller that its evidence is incomplete.

    Secret-bearing files are skipped entirely (S3) and are NOT reported as unscanned — excluding
    them is a deliberate policy decision, not a failure to read.
    """
    entries: list[CorpusEntry] = []
    unscanned: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if is_secret_bearing_file(path):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            unscanned.append(str(path))
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            unscanned.append(str(path))
            continue

        enclosing_call_by_node_id: dict[int, str | None] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _call_func_name(node)
                for arg in (*node.args, *(kw.value for kw in node.keywords)):
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        enclosing_call_by_node_id[id(arg)] = name

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            raw = node.value
            if not raw or looks_like_secret(raw):
                continue  # S3: secret-shaped values never enter the corpus
            context = _classify_context(raw, enclosing_call_by_node_id.get(id(node)))
            entries.append(
                CorpusEntry(
                    raw=raw,
                    tokens=_tokenize(raw),
                    context=context,
                    file=str(path),
                    line=node.lineno,
                )
            )
    return CorpusScan(entries=entries, unscanned=tuple(unscanned))


def build_corpus(root: Path) -> list[CorpusEntry]:
    """The corpus entries alone — the LOSSY view (it cannot report an incomplete scan).

    Kept for callers that only want to read/report literals. Anything deciding whether a symbol may
    be deleted must use :func:`build_corpus_scan` and pass the whole scan to :func:`quarantines`.
    """
    return build_corpus_scan(root).entries


def quarantines(symbol: str, corpus: "CorpusScan | list[CorpusEntry]") -> bool:
    """B20: does ``symbol`` quarantine (survive deletion) against this corpus?

    True when some entry contains ``symbol`` as a whole dispatch token *and* that entry's context is
    ``"dispatch"``. Log/prose-context matches are recorded implicitly by the corpus but never
    quarantine on their own.

    Given a :class:`CorpusScan` whose scan was INCOMPLETE, this returns True unconditionally: the
    corpus could not be fully enumerated, so no answer of "this symbol is not string-referenced"
    is available and the protective verdict is the only honest one. Passing a bare entry list
    asserts, by omission, that the scan was complete.
    """
    if isinstance(corpus, CorpusScan):
        if not corpus.complete:
            return True
        corpus = corpus.entries
    return any(symbol in entry.tokens and entry.context == "dispatch" for entry in corpus)


def format_report_line(entry: CorpusEntry) -> str:
    """Render one corpus entry as a report line, with secret-shaped text redacted (S3)."""
    return f"{entry.file}:{entry.line}: {redact(entry.raw)}"


def ledger_entry(symbol: str, entry: CorpusEntry) -> dict:
    """Render one ledger record binding ``symbol`` to a corpus entry, redacted (S3)."""
    return {
        "symbol": symbol,
        "file": entry.file,
        "line": entry.line,
        "raw": redact(entry.raw),
    }
