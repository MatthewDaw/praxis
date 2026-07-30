"""R8 acceptance: af-clean's deterministic detector census as the LLM-judgment allocation
function.

af-clean must run the deterministic detector census repo-wide FIRST and use per-file slop
density as the allocation function for LLM judgment -- sending only hotspots to the model
rather than visiting every file (B7), and publish an instrument x pattern matrix naming each
slop pattern's deterministic instrument or the literal marker "uninstrumented - judgment" for
a pattern with no detector (B8).

Acceptance (verbatim from the ticket): the report states total source file count and
LLM-judged file count, the judged set is bounded to files carrying at least one detector
finding or a density score above the stated threshold, and the report includes a matrix
naming each slop pattern with its instrument or the marker 'uninstrumented - judgment'.
"""

from __future__ import annotations

from pathlib import Path

from agent_factory.af_clean.census import (
    UNINSTRUMENTED_MARKER,
    discover_source_files,
    instrument_matrix,
    run_census,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_repo(tmp_path: Path) -> Path:
    """A small repo: one file a detector will flag (hotspot), several clean files that no
    detector flags and whose density stays at/under the threshold."""
    repo = tmp_path / "repo"
    _write(repo / "hotspot.py", "def unused():\n    pass\n" * 3)
    for i in range(4):
        _write(repo / f"clean_{i}.py", "def used():\n    return 1\n" * 20)
    _write(repo / "node_modules" / "vendored.js", "var x = 1;\n")  # excluded vendor dir
    return repo


def _fake_runner(finding_files: dict[str, int]):
    def _runner(repo_root: str, files: list[str]) -> dict[str, int]:
        return dict(finding_files)
    return _runner


def test_discover_source_files_excludes_vendored_dirs(tmp_path):
    repo = _make_repo(tmp_path)
    files = discover_source_files(str(repo))
    assert "hotspot.py" in files
    assert all("node_modules" not in f for f in files)


def test_instrument_matrix_names_every_pattern_with_instrument_or_marker():
    matrix = instrument_matrix()
    assert matrix  # non-empty: the ticket's own patterns are declared
    # Judgment-only patterns per B8 (no deterministic detector exists for these).
    for judgment_only in ("comment_terseness", "single_responsibility", "same_job_identity"):
        assert matrix[judgment_only] == UNINSTRUMENTED_MARKER
    # Detector-backed patterns name a real instrument, never the marker.
    for measured in ("dead_code", "duplication", "complexity"):
        assert matrix[measured] != UNINSTRUMENTED_MARKER
        assert matrix[measured]


def test_report_states_total_and_judged_file_counts_and_judged_is_materially_smaller(tmp_path):
    repo = _make_repo(tmp_path)
    report = run_census(
        str(repo),
        density_threshold=0.5,
        detector_runners={"vulture": _fake_runner({"hotspot.py": 2})},
    )
    assert report.total_source_file_count == 5  # hotspot + 4 clean, vendored excluded
    assert report.judged_file_count == 1
    assert report.judged_files == ["hotspot.py"]
    # B7 accept: judged count is materially smaller than the total.
    assert report.judged_file_count < report.total_source_file_count


def test_judged_set_bounded_to_finding_or_density_above_threshold(tmp_path):
    repo = _make_repo(tmp_path)
    # No detector finding anywhere, but clean_0.py's synthetic density is pushed above the
    # threshold via a runner reporting a lone finding there -- it must join the judged set
    # purely on the density clause, and files below threshold with zero findings must not.
    report = run_census(
        str(repo),
        density_threshold=0.001,
        detector_runners={"vulture": _fake_runner({"clean_0.py": 1})},
    )
    assert "clean_0.py" in report.judged_files
    for f in ("clean_1.py", "clean_2.py", "clean_3.py"):
        assert f not in report.judged_files


def test_report_dict_includes_matrix_and_counts(tmp_path):
    repo = _make_repo(tmp_path)
    report = run_census(str(repo), detector_runners={"vulture": _fake_runner({})})
    d = report.to_dict()
    assert d["total_source_file_count"] == report.total_source_file_count
    assert d["judged_file_count"] == report.judged_file_count
    assert d["matrix"]["comment_terseness"] == UNINSTRUMENTED_MARKER
    assert d["matrix"]["dead_code"] == "vulture"


def test_no_detectors_available_degrades_without_crashing(tmp_path):
    """A detector that is absent must never crash the census -- its findings are simply
    zero, not an exception (graceful degradation per the requirements doc)."""
    repo = _make_repo(tmp_path)
    report = run_census(str(repo), detector_runners={})
    assert report.total_source_file_count == 5
    assert isinstance(report.judged_file_count, int)
