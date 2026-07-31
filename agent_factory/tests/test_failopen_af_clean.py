"""Fail-open sweep — af-clean's protective evidence must never read "unknown" as "nothing found".

Four instances, all the same shape: a gate whose input could not be gathered reported the
permissive answer instead of tripping.

* ``producers.scar_findings`` compared the detector's verdict against ``"scar"``, a string
  ``detect_scar`` never returns (it returns ``"advisory"`` / ``"eligible"``), so the R23/B21 scar
  guard emitted nothing, ever — and the ``is_scar`` must-not-happen refusal in the witness gate,
  which keys on the rule name only this producer emits, could never fire. It also swallowed a
  blame failure into ``continue``, dropping the protection entirely.
* ``reachability.build_symbol_graph`` silently skipped unreadable/unparseable files, dropping their
  call edges — so a symbol whose only caller lived in such a file looked caller-less and became a
  DELETE candidate.
* ``af_clean_string_corpus.build_corpus`` did the same to the B6 string-dispatch corpus, whose
  entire job is to prove a symbol IS referenced.
* ``af_clean_validate``'s building-validation lane skipped a check with no ``run`` command and
  still reported the lane PASSED — an unrunnable check counted as satisfied.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_factory.af_clean.producers import scar_findings
from agent_factory.af_clean.reachability import (
    INCOMPLETE_SYMBOL_GRAPH,
    Symbol,
    SymbolGraph,
    build_symbol_graph,
    compute_reachability,
)
from agent_factory.af_clean_string_corpus import (
    CorpusScan,
    build_corpus,
    build_corpus_scan,
    quarantines,
)
from agent_factory.af_clean_validate import FAILED, PASSED, run_validation_and_remediation


# --------------------------------------------------------------------------- scar guard


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _repo_with_fix_commit(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "m.py").write_text("def f():\n    try:\n        g()\n    except Exception:\n        pass\n")
    _git(repo, "add", "m.py")
    _git(repo, "commit", "-q", "-m", "fix: guard against the crash in #42")
    return repo


def test_scar_finding_is_actually_emitted_for_a_bugfix_commit(tmp_path):
    """The guard was dead: it matched the verdict against a string the detector never returns."""
    repo = _repo_with_fix_commit(tmp_path)
    out = scar_findings(repo, [("m.py", 2)])
    assert len(out) == 1, "a construct introduced by a fix commit must produce a KEEP finding"
    assert out[0].rule == "defensive-code-is-a-scar"
    assert "KEEP" in (out[0].proposal or "")


def test_scar_finding_absent_for_an_ordinary_commit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "m.py").write_text("def f():\n    return 1\n")
    _git(repo, "add", "m.py")
    _git(repo, "commit", "-q", "-m", "add the thing")
    assert scar_findings(repo, [("m.py", 2)]) == []


def test_unrunnable_blame_protects_rather_than_drops(tmp_path):
    """No git repo at all: scar status is UNKNOWN, which must protect, not silently clear."""
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    (not_a_repo / "m.py").write_text("x = 1\n")
    out = scar_findings(not_a_repo, [("m.py", 1)])
    assert len(out) == 1
    assert "unverified" in (out[0].proposal or "").lower()


# --------------------------------------------------------------------------- symbol graph


def test_unparseable_file_is_recorded_not_silently_dropped(tmp_path):
    (tmp_path / "ok.py").write_text("def caller():\n    helper()\n")
    (tmp_path / "broken.py").write_text("def oops(:\n")
    graph = build_symbol_graph(tmp_path)
    assert "broken.py" in graph.unparsed


def test_incomplete_graph_classifies_nothing_unreachable(tmp_path):
    """Missing files mean missing call edges, so 'no caller' is unproven — refuse to purge."""
    graph = SymbolGraph(
        symbols={"a.py::helper": Symbol(id="a.py::helper", file_path="a.py")},
        unparsed=("broken.py",),
    )
    result = compute_reachability(graph, surfaces=("a.py::surface",))
    assert result.unreachable == frozenset()
    assert result.reachable == frozenset(graph.symbols)
    assert INCOMPLETE_SYMBOL_GRAPH in (result.note or "")
    assert "broken.py" in (result.note or "")


def test_complete_graph_still_classifies_unreachable(tmp_path):
    """The guard must not blunt the normal path: a clean scan still finds dead code."""
    graph = SymbolGraph(symbols={"a.py::dead": Symbol(id="a.py::dead", file_path="a.py")})
    result = compute_reachability(graph, surfaces=("a.py::surface",))
    assert result.unreachable == frozenset({"a.py::dead"})


# --------------------------------------------------------------------------- string corpus


def test_corpus_scan_records_unparseable_files(tmp_path):
    (tmp_path / "ok.py").write_text('CMD = "hooks/build_gate.py"\n')
    (tmp_path / "broken.py").write_text("def oops(:\n")
    scan = build_corpus_scan(tmp_path)
    assert not scan.complete
    assert any("broken.py" in p for p in scan.unscanned)
    # The lossy view still works for callers that only want the literals.
    assert len(build_corpus(tmp_path)) == len(scan.entries)


def test_incomplete_corpus_quarantines_every_symbol(tmp_path):
    """"Not in the corpus" from a partial scan is not evidence the symbol is unreferenced."""
    (tmp_path / "broken.py").write_text("def oops(:\n")
    scan = build_corpus_scan(tmp_path)
    assert quarantines("anything_at_all", scan) is True


def test_complete_corpus_still_answers_honestly(tmp_path):
    (tmp_path / "ok.py").write_text('CMD = "hooks/build_gate.py"\n')
    scan = build_corpus_scan(tmp_path)
    assert scan.complete
    assert quarantines("build_gate", scan) is True
    assert quarantines("never_mentioned", scan) is False
    # A bare entry list asserts completeness by omission and keeps the old behaviour.
    assert quarantines("never_mentioned", scan.entries) is False
    assert isinstance(scan, CorpusScan)


# --------------------------------------------------------------------------- validation lane


class _Proc:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


def _pass_runner(argv, cwd):
    return _Proc(0)


def test_check_with_no_run_command_fails_the_lane(tmp_path):
    """An un-executable check cannot be counted as passed — it fails the lane and is named."""
    checks = [{"meta": {"check_id": "chk-runnable", "run": "true"}},
              {"meta": {"check_id": "chk-broken"}}]
    report = run_validation_and_remediation(
        tmp_path, runner=_pass_runner, commands={}, building_validation_checks=checks
    )
    lane = [p for p in report.phases if p.name == "building_validation_lane"][0]
    assert lane.status == FAILED
    broken = [r for r in report.building_validation_results if r.name == "chk-broken"][0]
    assert broken.status == FAILED
    assert any("chk-broken" in w for w in report.warnings)


def test_all_runnable_checks_still_pass_the_lane(tmp_path):
    checks = [{"meta": {"check_id": "chk-1", "run": "true"}}]
    report = run_validation_and_remediation(
        tmp_path, runner=_pass_runner, commands={}, building_validation_checks=checks
    )
    lane = [p for p in report.phases if p.name == "building_validation_lane"][0]
    assert lane.status == PASSED


# --------------------------------------------------------------------------- commit-stack validation

_APPLIER_SLOP = '''class Widget:
    def increment_counter(self, counter):
        # increment counter
        counter = counter + 1
        return counter
'''


def _endorsing_verifier(argv, **kwargs):
    class _R:
        stdout = '{"endorsed_hunk_ids": ["h1"]}'
        returncode = 0
    return _R()


def _committed_repo(tmp_path: Path) -> Path:
    p = tmp_path / "src" / "widget.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_APPLIER_SLOP, encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_missing_validate_fn_is_recorded_not_silently_substituted(tmp_path):
    """B25's post-commit validation defaulted to a constant True. It still does when no validator
    is supplied, but the stand-down is now RECORDED so nobody reads it as 'validation passed'."""
    from agent_factory.af_clean.applier import apply_findings
    from agent_factory.af_clean.findings import Finding, Location

    repo = _committed_repo(tmp_path)
    f = Finding(rule="comment-no-information-gain", tier="advise",
                location=Location(file="src/widget.py", line=3), pole="bloat")
    out = apply_findings(repo, [f], verifier_runner=_endorsing_verifier)
    assert out.applied, "the finding should still land"
    assert any("COMMIT-STACK VALIDATION SKIPPED" in reason for _f, reason in out.reported)


def test_supplied_validate_fn_is_not_flagged(tmp_path):
    from agent_factory.af_clean.applier import apply_findings
    from agent_factory.af_clean.findings import Finding, Location

    repo = _committed_repo(tmp_path)
    f = Finding(rule="comment-no-information-gain", tier="advise",
                location=Location(file="src/widget.py", line=3), pole="bloat")
    out = apply_findings(repo, [f], verifier_runner=_endorsing_verifier,
                         validate_fn=lambda _p: True)
    assert not any("COMMIT-STACK VALIDATION SKIPPED" in reason for _f, reason in out.reported)
