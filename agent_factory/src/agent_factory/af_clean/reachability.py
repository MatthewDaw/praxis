"""The pre-clean read-only measurement pass and tri-state deletion verdict for af-clean (R19).

Ties together B44 (pre-clean measurement), B16/B43 (reachability, code-derived primary with
Praxis-surface enrichment secondary, exemption governs editability not visibility), B17 (the
tri-state verdict grid), B18 (test deletion as a first-class, bound, atomic-unit output), and B19
(staged excision to a fixed point) into one module. See
``docs/brainstorms/2026-07-29-af-clean-requirements.md`` §2.1/§2.4 for the requirement text this
operationalizes.

Reachability here is a **direct-caller** check, not a one-shot transitive closure from roots: a
symbol counts as reachable this round if it IS a root/surface/exempt-caller, or if at least one
OTHER symbol currently in the graph calls it -- regardless of whether that caller is itself
reachable. This is deliberately weaker than full graph reachability in any single round, which is
exactly why :func:`stage_excision_to_fixed_point` recomputes after every purge round (B19): a
helper whose only caller is itself deleted in round 1 (because that caller had no callers of its
own) is missed by round 1's direct-caller check and is only exposed -- and caught -- once round 2
recomputes reachability against the smaller, post-deletion graph.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

# B16: on an empty surface set, af-clean must say so rather than silently treating the absence
# as "everything is unreachable".
NO_SURFACE_ORACLE_AVAILABLE = "no-surface-oracle-available"

# The B17 tri-state verdict grid's four cells.
KEEP = "keep"
KEEP_TEST_DEBT = "keep_test_debt"
DELETE = "delete"
QUARANTINE = "quarantine"

_EXCLUDED_DIR_NAMES = {
    "node_modules", ".venv", "venv", "__pycache__", ".git",
    "dist", "build", ".mypy_cache", ".pytest_cache",
}


# --------------------------------------------------------------------------- the symbol graph

@dataclass(frozen=True)
class Symbol:
    """One statically-discovered function/method: its id, defining file, the (best-effort
    resolved) ids of every symbol it calls, whether it is itself a test, and its source line span
    (used to attribute per-line coverage evidence back to the enclosing symbol)."""

    id: str
    file_path: str
    calls: tuple[str, ...] = ()
    is_test: bool = False
    lineno: int = 0
    end_lineno: int = 0


@dataclass
class SymbolGraph:
    """The full repo call graph: every discovered symbol, keyed by id (``"{file}::{qualname}"``)."""

    symbols: dict[str, Symbol] = field(default_factory=dict)

    def callers_of(self, symbol_id: str) -> list[str]:
        return sorted(s.id for s in self.symbols.values() if symbol_id in s.calls)

    def symbol_at(self, file_path: str, line_no: int) -> Optional[str]:
        """The innermost symbol whose source span contains ``line_no`` in ``file_path``, or
        ``None`` if no defined symbol covers that line (module-level code, blank lines, ...)."""
        best: Optional[str] = None
        best_span: Optional[int] = None
        for sym in self.symbols.values():
            if sym.file_path != file_path:
                continue
            end = sym.end_lineno or sym.lineno
            if sym.lineno <= line_no <= end:
                span = end - sym.lineno
                if best_span is None or span < best_span:
                    best, best_span = sym.id, span
        return best


def _is_excluded_dir(name: str) -> bool:
    return name in _EXCLUDED_DIR_NAMES or name.startswith(".")


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            fn = child.func
            if isinstance(fn, ast.Name):
                names.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                names.add(fn.attr)
    return names


def build_symbol_graph(repo_root: "str | Path") -> SymbolGraph:
    """Build the repo-wide call graph from every ``.py`` file under ``repo_root`` -- including
    exempt paths (B43: exemption governs editability, never visibility, so exempt files must
    always be parsed and always contribute reachability edges/roots)."""
    root = Path(repo_root)
    graph = SymbolGraph()
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _is_excluded_dir(d)]
        for name in filenames:
            if name.endswith(".py"):
                files.append(Path(dirpath) / name)

    parsed: dict[str, tuple[ast.AST, str]] = {}
    defs_by_name: dict[str, list[str]] = {}
    for f in sorted(files):
        rel = f.relative_to(root).as_posix()
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(text)
        except (OSError, SyntaxError):
            continue
        parsed[rel] = (tree, text)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs_by_name.setdefault(node.name, []).append(f"{rel}::{node.name}")

    for rel, (tree, _text) in parsed.items():
        file_name = Path(rel).name
        is_test_file = file_name.startswith("test_") or file_name.endswith("_test.py")
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sym_id = f"{rel}::{node.name}"
                resolved: set[str] = set()
                for name in _called_names(node):
                    resolved.update(defs_by_name.get(name, []))
                resolved.discard(sym_id)  # a symbol's own recursive call is not an external edge
                graph.symbols[sym_id] = Symbol(
                    id=sym_id,
                    file_path=rel,
                    calls=tuple(sorted(resolved)),
                    is_test=is_test_file and node.name.startswith("test_"),
                    lineno=node.lineno,
                    end_lineno=getattr(node, "end_lineno", node.lineno),
                )
    return graph


# --------------------------------------------------------------------------- B44: coverage collection

@dataclass
class CoverageMap:
    """Read-only measurement evidence: symbol id -> the ids of every test observed exercising it."""

    covered_by: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def is_covered(self, symbol_id: str) -> bool:
        return bool(self.covered_by.get(symbol_id))

    def covering_tests(self, symbol_id: str) -> tuple[str, ...]:
        return self.covered_by.get(symbol_id, ())


CommandRunner = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]


def default_runner(argv: list[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=1800)


def collect_coverage(
    repo_root: "str | Path",
    *,
    graph: Optional[SymbolGraph] = None,
    runner: CommandRunner = default_runner,
    command: Optional[list[str]] = None,
) -> CoverageMap:
    """The B44 pre-clean read-only measurement pass.

    Runs the coverage-collection command (default: pytest with per-test coverage contexts) and
    returns which symbols each test exercised. This is MEASUREMENT, never remediation: the report
    is written to a throwaway tempdir OUTSIDE ``repo_root`` (never into the target repo), and
    nothing in this function ever opens a file inside ``repo_root`` for writing -- ``repo_root``
    itself is read via the injected ``runner`` (a subprocess) and the resulting report only.
    Degrades to an empty :class:`CoverageMap` -- never raises -- on any absence/failure (no
    pytest-cov installed, a crashing subprocess, an unparsable report), matching af-clean's
    graceful-degradation posture elsewhere in this package.
    """
    root = Path(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        report_path = Path(tmp) / "coverage-contexts.json"
        argv = command or [
            "python", "-m", "pytest", "-q",
            "--cov=.", "--cov-context=test",
            f"--cov-report=json:{report_path}",
        ]
        try:
            runner(argv, root)
        except (OSError, subprocess.SubprocessError):
            return CoverageMap()
        if not report_path.is_file():
            return CoverageMap()
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return CoverageMap()

    covered_by: dict[str, set[str]] = {}
    for file_path, file_data in (data.get("files") or {}).items():
        contexts = file_data.get("contexts") or {}
        for line_no_str, ctx_list in contexts.items():
            try:
                line_no = int(line_no_str)
            except ValueError:
                continue
            symbol_id = graph.symbol_at(file_path, line_no) if graph else f"{file_path}:{line_no}"
            if symbol_id is None:
                continue
            for ctx in ctx_list:
                test_id = ctx.split("|", 1)[0]
                if test_id:
                    covered_by.setdefault(symbol_id, set()).add(test_id)
    return CoverageMap({k: tuple(sorted(v)) for k, v in covered_by.items()})


# --------------------------------------------------------------------------- B16/B43: reachability

def _is_exempt(file_path: str, exempt_paths: Iterable[str]) -> bool:
    """True iff ``file_path`` is exactly, or nested under, one of ``exempt_paths`` (each either a
    bare file path or a directory path -- with or without a trailing slash)."""
    norm = file_path.replace("\\", "/")
    for entry in exempt_paths:
        e = entry.replace("\\", "/").rstrip("/")
        if not e:
            continue
        if norm == e or norm.startswith(e + "/"):
            return True
    return False


@dataclass
class ReachabilityResult:
    reachable: "frozenset[str]"
    unreachable: "frozenset[str]"
    surface_oracle_available: bool
    note: Optional[str] = None


def compute_reachability(
    graph: SymbolGraph,
    *,
    roots: Iterable[str] = (),
    surfaces: Iterable[str] = (),
    exempt_paths: Iterable[str] = (),
) -> ReachabilityResult:
    """B16/B43: reachability is code-derived primary, Praxis-surface enrichment secondary.

    An EMPTY surface set means no surface oracle is available at all -- af-clean must say so
    (:data:`NO_SURFACE_ORACLE_AVAILABLE`) and must not classify a single symbol unreachable on
    that basis (a purge on an empty surface set is exactly the §3.2 must-not-happen case B16
    guards).

    When surfaces ARE present: a symbol is reachable if it is a root, a surface, defined inside an
    exempt path (exempt code is always parsed and always contributes reachability edges/roots --
    B43 -- so e.g. a symbol called only from ``migrations/`` is reachable), or is called by ANY
    other symbol currently in the graph (regardless of that caller's own reachability -- see the
    module docstring on why this is a direct-caller check, not a one-shot transitive closure).
    """
    surfaces = list(surfaces)
    if not surfaces:
        return ReachabilityResult(
            reachable=frozenset(graph.symbols),
            unreachable=frozenset(),
            surface_oracle_available=False,
            note=NO_SURFACE_ORACLE_AVAILABLE,
        )

    live = set(roots) | set(surfaces)
    for sym in graph.symbols.values():
        if _is_exempt(sym.file_path, exempt_paths):
            live.add(sym.id)

    reachable: set[str] = set()
    for sym_id, sym in graph.symbols.items():
        if sym_id in live:
            reachable.add(sym_id)
            continue
        if any(sym_id in caller.calls for caller in graph.symbols.values()):
            reachable.add(sym_id)

    unreachable = set(graph.symbols) - reachable
    return ReachabilityResult(frozenset(reachable), frozenset(unreachable), True)


# --------------------------------------------------------------------------- B17: the tri-state verdict

@dataclass(frozen=True)
class VerdictEntry:
    symbol_id: str
    reachable: bool
    covered: bool
    verdict: str
    covering_tests: tuple[str, ...] = ()


def classify(
    graph: SymbolGraph,
    reachability: ReachabilityResult,
    coverage: CoverageMap,
) -> list[VerdictEntry]:
    """B17's tri-state verdict grid, applied to every non-test symbol in ``graph``:

    |            | Covered              | Uncovered                |
    |------------|----------------------|--------------------------|
    | Reachable  | keep                 | keep + record test debt  |
    | Unreachable| delete symbol + test | quarantine               |
    """
    entries: list[VerdictEntry] = []
    for sym_id, sym in graph.symbols.items():
        if sym.is_test:
            continue
        reachable = sym_id in reachability.reachable
        tests = coverage.covering_tests(sym_id)
        covered = bool(tests)
        if reachable:
            verdict = KEEP if covered else KEEP_TEST_DEBT
        else:
            verdict = DELETE if covered else QUARANTINE
        entries.append(VerdictEntry(sym_id, reachable, covered, verdict, tests))
    return entries


# --------------------------------------------------------------------------- B18: bound test deletion

@dataclass(frozen=True)
class BindingRecord:
    """A machine-readable record naming the symbol a deleted test was bound to -- the atomic unit
    B18 requires: one (symbol, exclusively-covering-tests) pair, reviewable as one hunk group."""

    symbol_id: str
    test_id: str


class UnboundTestDeletionRejected(ValueError):
    """Raised when a test deletion is proposed without a recorded binding to a deleted symbol."""


def plan_test_deletions(
    entries: Iterable[VerdictEntry],
) -> tuple[list[BindingRecord], list[str]]:
    """Bind each DELETE-verdict symbol to its EXCLUSIVELY-covering tests.

    A covering test is only bound (and thus only deletable) if every symbol it covers is itself a
    DELETE-verdict symbol -- a test that also covers a kept/quarantined symbol is not exclusive to
    the deletion and must survive. Returns ``(bindings, kept_test_ids)`` where ``kept_test_ids``
    lists tests that cover a to-be-deleted symbol but are retained because they are not exclusive.
    """
    entries = list(entries)
    delete_symbol_ids = {e.symbol_id for e in entries if e.verdict == DELETE}

    test_to_symbols: dict[str, set[str]] = {}
    for e in entries:
        for test_id in e.covering_tests:
            test_to_symbols.setdefault(test_id, set()).add(e.symbol_id)

    bindings: list[BindingRecord] = []
    kept: list[str] = []
    for e in entries:
        if e.verdict != DELETE:
            continue
        for test_id in e.covering_tests:
            covered_symbols = test_to_symbols.get(test_id, set())
            if covered_symbols <= delete_symbol_ids:
                bindings.append(BindingRecord(symbol_id=e.symbol_id, test_id=test_id))
            elif test_id not in kept:
                kept.append(test_id)
    return bindings, sorted(kept)


def enforce_test_deletion_binding(
    test_id: str,
    symbol_id: str,
    bindings: Iterable[BindingRecord],
) -> None:
    """The per-pair guard B18 describes: raises :class:`UnboundTestDeletionRejected` unless a
    binding record names EXACTLY this ``(symbol_id, test_id)`` pair. Callable standalone as the
    final gate immediately before a test is actually removed from disk -- so an unbound test
    deletion is auto-rejected even if some upstream planning step got it wrong."""
    for b in bindings:
        if b.test_id == test_id and b.symbol_id == symbol_id:
            return
    raise UnboundTestDeletionRejected(
        f"deleting test {test_id!r} is refused: no binding record names it against "
        f"unreachable symbol {symbol_id!r}"
    )


# --------------------------------------------------------------------------- B19: staged excision

@dataclass(frozen=True)
class RoundResult:
    round_number: int
    deleted: tuple[str, ...]
    quarantined: tuple[str, ...]
    bindings: tuple[BindingRecord, ...]


@dataclass
class ExcisionReport:
    rounds: list[RoundResult] = field(default_factory=list)
    surface_oracle_available: bool = True

    @property
    def all_deleted(self) -> list[str]:
        return [s for r in self.rounds for s in r.deleted]

    @property
    def all_quarantined(self) -> list[str]:
        # Only the FINAL round's quarantine set is live: a symbol quarantined in an early round
        # can still be caught for deletion once a later round's purge exposes a fresh binding.
        return list(self.rounds[-1].quarantined) if self.rounds else []


def stage_excision_to_fixed_point(
    graph: SymbolGraph,
    *,
    roots: Iterable[str] = (),
    surfaces: Iterable[str] = (),
    exempt_paths: Iterable[str] = (),
    coverage: Optional[CoverageMap] = None,
    max_rounds: int = 25,
) -> ExcisionReport:
    """B19: recompute reachability after each purge round until a fixed point.

    Each round computes reachability + the tri-state verdict fresh against the CURRENT remaining
    graph, deletes every DELETE-verdict symbol (and removes it -- and any now-dangling call edges
    to it -- from the graph before the next round), and stops once a round proposes zero
    deletions. A helper reachable only through a symbol deleted in round 1 is therefore exposed --
    and caught -- in round 2, not left for the next invocation.
    """
    coverage = coverage or CoverageMap()
    remaining = dict(graph.symbols)
    report = ExcisionReport()

    for round_number in range(1, max_rounds + 1):
        round_graph = SymbolGraph(symbols=remaining)
        reachability = compute_reachability(
            round_graph, roots=roots, surfaces=surfaces, exempt_paths=exempt_paths,
        )
        report.surface_oracle_available = reachability.surface_oracle_available
        entries = classify(round_graph, reachability, coverage)
        deletions = [e for e in entries if e.verdict == DELETE]
        quarantined = tuple(sorted(e.symbol_id for e in entries if e.verdict == QUARANTINE))

        if not deletions:
            report.rounds.append(RoundResult(round_number, (), quarantined, ()))
            break

        bindings, _kept = plan_test_deletions(entries)
        for binding in bindings:
            enforce_test_deletion_binding(binding.test_id, binding.symbol_id, bindings)

        deleted_ids = tuple(sorted(e.symbol_id for e in deletions))
        report.rounds.append(RoundResult(round_number, deleted_ids, quarantined, tuple(bindings)))

        deleted_test_ids = {b.test_id for b in bindings}
        deleted_set = set(deleted_ids)
        for sym_id in deleted_ids:
            remaining.pop(sym_id, None)
        for test_sym_id in list(remaining):
            if test_sym_id in deleted_test_ids:
                remaining.pop(test_sym_id, None)
        for sym_id, sym in list(remaining.items()):
            if any(c in deleted_set for c in sym.calls):
                remaining[sym_id] = Symbol(
                    id=sym.id,
                    file_path=sym.file_path,
                    calls=tuple(c for c in sym.calls if c not in deleted_set),
                    is_test=sym.is_test,
                    lineno=sym.lineno,
                    end_lineno=sym.end_lineno,
                )
    else:
        # max_rounds exhausted without reaching a fixed point -- record what the last round saw
        # rather than silently stopping; callers can inspect len(report.rounds) == max_rounds.
        pass

    return report
