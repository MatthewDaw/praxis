"""R19 acceptance: the pre-clean measurement pass and the tri-state deletion verdict.

Acceptance (verbatim from the ticket): the pre-clean pass collects coverage with zero edits; an
empty surface set yields no-surface-oracle-available and zero unreachable classifications; a
symbol called only from exempt migrations/ is reachable; unreachable-and-covered deletes with its
exclusively-covering test carrying a binding record; unreachable-and-uncovered quarantines; an
unbound test deletion is auto-rejected; and a helper reachable only through a round-1 deletion is
caught in round 2.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from agent_factory.af_clean.reachability import (
    DELETE,
    KEEP,
    KEEP_TEST_DEBT,
    NO_SURFACE_ORACLE_AVAILABLE,
    QUARANTINE,
    BindingRecord,
    CoverageMap,
    Symbol,
    SymbolGraph,
    UnboundTestDeletionRejected,
    build_symbol_graph,
    classify,
    collect_coverage,
    compute_reachability,
    enforce_test_deletion_binding,
    plan_test_deletions,
    stage_excision_to_fixed_point,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for p in sorted(root.rglob("*.py")):
        digest.update(p.read_bytes())
    return digest.hexdigest()


# --------------------------------------------------------------------------- B44: zero-edit coverage

def test_collect_coverage_makes_zero_edits_to_the_repo(tmp_path):
    repo = tmp_path / "repo"
    _write(repo / "pkg" / "app.py", "def alive():\n    return 1\n")
    before = _hash_tree(repo)

    def stub_runner(argv, cwd):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    result = collect_coverage(repo, runner=stub_runner)

    assert isinstance(result, CoverageMap)
    assert _hash_tree(repo) == before  # not a single source byte changed


def test_collect_coverage_degrades_to_empty_map_on_failure(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def failing_runner(argv, cwd):
        raise OSError("no pytest installed")

    result = collect_coverage(repo, runner=failing_runner)
    assert result.covered_by == {}


# --------------------------------------------------------------------------- B16: empty surface set

def test_empty_surface_set_yields_no_surface_oracle_and_zero_unreachable():
    graph = SymbolGraph(symbols={
        "a.py::used": Symbol(id="a.py::used", file_path="a.py"),
        "a.py::orphan": Symbol(id="a.py::orphan", file_path="a.py"),
    })

    result = compute_reachability(graph, roots=(), surfaces=(), exempt_paths=())

    assert result.surface_oracle_available is False
    assert result.note == NO_SURFACE_ORACLE_AVAILABLE
    assert result.unreachable == frozenset()
    assert result.reachable == frozenset(graph.symbols)


# --------------------------------------------------------------------------- B43: exempt callers

def test_symbol_called_only_from_exempt_migrations_is_reachable():
    graph = SymbolGraph(symbols={
        "migrations/0001_init.py::upgrade": Symbol(
            id="migrations/0001_init.py::upgrade",
            file_path="migrations/0001_init.py",
            calls=("app/helpers.py::apply_schema",),
        ),
        "app/helpers.py::apply_schema": Symbol(
            id="app/helpers.py::apply_schema", file_path="app/helpers.py",
        ),
    })

    result = compute_reachability(
        graph, roots=(), surfaces=("some-surface",), exempt_paths=("migrations/",),
    )

    assert "app/helpers.py::apply_schema" in result.reachable
    assert "migrations/0001_init.py::upgrade" in result.reachable


# --------------------------------------------------------------------------- B17: the tri-state grid

def test_tri_state_grid_all_four_cells():
    graph = SymbolGraph(symbols={
        "a.py::kept": Symbol(id="a.py::kept", file_path="a.py"),
        "a.py::debt": Symbol(id="a.py::debt", file_path="a.py"),
        "a.py::dead_covered": Symbol(id="a.py::dead_covered", file_path="a.py"),
        "a.py::dead_uncovered": Symbol(id="a.py::dead_uncovered", file_path="a.py"),
    })
    from agent_factory.af_clean.reachability import ReachabilityResult
    reach = ReachabilityResult(
        reachable=frozenset({"a.py::kept", "a.py::debt"}),
        unreachable=frozenset({"a.py::dead_covered", "a.py::dead_uncovered"}),
        surface_oracle_available=True,
    )
    coverage = CoverageMap(covered_by={
        "a.py::kept": ("tests/test_a.py::test_kept",),
        "a.py::dead_covered": ("tests/test_a.py::test_dead",),
    })

    entries = {e.symbol_id: e for e in classify(graph, reach, coverage)}

    assert entries["a.py::kept"].verdict == KEEP
    assert entries["a.py::debt"].verdict == KEEP_TEST_DEBT
    assert entries["a.py::dead_covered"].verdict == DELETE
    assert entries["a.py::dead_uncovered"].verdict == QUARANTINE


# --------------------------------------------------------------------------- B18: bound test deletion

def test_unreachable_and_covered_deletes_with_binding_record():
    graph = SymbolGraph(symbols={
        "a.py::dead": Symbol(id="a.py::dead", file_path="a.py"),
    })
    from agent_factory.af_clean.reachability import ReachabilityResult
    reach = ReachabilityResult(
        reachable=frozenset(), unreachable=frozenset({"a.py::dead"}), surface_oracle_available=True,
    )
    coverage = CoverageMap(covered_by={"a.py::dead": ("tests/test_a.py::test_dead",)})

    entries = classify(graph, reach, coverage)
    bindings, kept = plan_test_deletions(entries)

    assert bindings == [BindingRecord(symbol_id="a.py::dead", test_id="tests/test_a.py::test_dead")]
    assert kept == []
    # the guard accepts exactly this bound pair
    enforce_test_deletion_binding("tests/test_a.py::test_dead", "a.py::dead", bindings)


def test_shared_test_is_not_deleted_and_survives_as_kept():
    graph = SymbolGraph(symbols={
        "a.py::dead": Symbol(id="a.py::dead", file_path="a.py"),
        "a.py::kept": Symbol(id="a.py::kept", file_path="a.py"),
    })
    from agent_factory.af_clean.reachability import ReachabilityResult
    reach = ReachabilityResult(
        reachable=frozenset({"a.py::kept"}),
        unreachable=frozenset({"a.py::dead"}),
        surface_oracle_available=True,
    )
    shared_test = ("tests/test_a.py::test_shared",)
    coverage = CoverageMap(covered_by={
        "a.py::dead": shared_test,
        "a.py::kept": shared_test,
    })

    entries = classify(graph, reach, coverage)
    bindings, kept = plan_test_deletions(entries)

    assert bindings == []
    assert kept == ["tests/test_a.py::test_shared"]


def test_unbound_test_deletion_is_auto_rejected():
    with pytest.raises(UnboundTestDeletionRejected):
        enforce_test_deletion_binding("tests/test_a.py::test_dead", "a.py::dead", bindings=[])


# --------------------------------------------------------------------------- B19: staged excision

def test_helper_reachable_only_through_round_1_deletion_is_caught_in_round_2():
    # `root` is a real, always-live entry point. `orphan_caller` has NO caller of its own (so it is
    # unreachable in round 1) but calls `helper`, which is therefore misclassified reachable in
    # round 1's direct-caller check. Once round 1 deletes `orphan_caller` (unreachable + covered),
    # round 2 must recompute and find `helper` has no remaining caller either.
    graph = SymbolGraph(symbols={
        "a.py::root": Symbol(id="a.py::root", file_path="a.py", calls=()),
        "a.py::orphan_caller": Symbol(
            id="a.py::orphan_caller", file_path="a.py", calls=("a.py::helper",),
        ),
        "a.py::helper": Symbol(id="a.py::helper", file_path="a.py"),
    })
    coverage = CoverageMap(covered_by={
        "a.py::orphan_caller": ("tests/test_a.py::test_orphan",),
        "a.py::helper": ("tests/test_a.py::test_helper",),
    })

    report = stage_excision_to_fixed_point(
        graph,
        roots=("a.py::root",),
        surfaces=("a.py::root",),
        exempt_paths=(),
        coverage=coverage,
    )

    assert len(report.rounds) >= 2
    assert "a.py::orphan_caller" in report.rounds[0].deleted
    assert "a.py::helper" not in report.rounds[0].deleted
    assert "a.py::helper" in report.rounds[1].deleted


def test_staged_excision_reaches_a_fixed_point_and_stops():
    graph = SymbolGraph(symbols={
        "a.py::root": Symbol(id="a.py::root", file_path="a.py"),
        "a.py::used": Symbol(id="a.py::used", file_path="a.py"),
    })
    report = stage_excision_to_fixed_point(
        graph, roots=("a.py::root",), surfaces=("a.py::root",), coverage=CoverageMap(),
    )
    # nothing here is ever unreachable, so the very first round proposes zero deletions
    assert len(report.rounds) == 1
    assert report.rounds[0].deleted == ()


# --------------------------------------------------------------------------- integration: real AST graph

def test_build_symbol_graph_from_real_files_and_full_pipeline(tmp_path):
    repo = tmp_path / "repo"
    _write(repo / "app.py", (
        "def main():\n"
        "    return helper()\n"
        "\n"
        "def helper():\n"
        "    return 1\n"
        "\n"
        "def dead():\n"
        "    return 2\n"
    ))
    _write(repo / "tests" / "test_app.py", (
        "def test_helper():\n"
        "    assert True\n"
    ))

    graph = build_symbol_graph(repo)
    assert "app.py::main" in graph.symbols
    assert "app.py::helper" in graph.symbols
    assert "app.py::dead" in graph.symbols
    assert "app.py::helper" in graph.symbols["app.py::main"].calls  # sanity: main calls helper

    reach = compute_reachability(
        graph, roots=("app.py::main",), surfaces=("app.py::main",), exempt_paths=(),
    )
    assert "app.py::main" in reach.reachable
    assert "app.py::helper" in reach.reachable
    assert "app.py::dead" in reach.unreachable
