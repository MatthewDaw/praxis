"""The E1 human path: producers + the ``python -m agent_factory.af_clean`` entry point.

These cover the gap that shipped with the original build set: the engine and the detectors both
existed and were tested, but nothing connected a typed command to them, so every one of the bugs
below was invisible to a green suite -- a manifest method iterated as if it were a list, a verdict
vocabulary that never matched, and comments judged without the signature they restate.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_factory.af_clean.producers import (
    _annotated_tokens,
    comment_findings,
    default_producer,
    iter_source_files,
)
from agent_factory.af_clean.findings import admit_finding


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


SLOP = '''class Widget:
    def increment_counter(self, counter):
        # increment counter
        counter = counter + 1
        # Guard against None because upstream sends None on the first poll and we
        # crashed in prod on 2024-03-02. See INC-441.
        if counter is None:
            return 0
        return counter
'''


def test_restating_comment_is_found_and_why_comment_is_protected(tmp_path):
    repo = _repo(tmp_path, {"src/widget.py": SLOP})
    findings = comment_findings(repo)
    lines = {f.location.line for f in findings}
    assert 3 in lines, "a comment restating its enclosing signature must be found"
    assert 5 not in lines and 6 not in lines, (
        "a comment carrying a reason and an incident reference is load-bearing and must survive")


def test_annotated_tokens_include_the_enclosing_signature():
    """The bug that made the detector find nothing: judged against only the next line, a comment
    restating its method scores 0.5 overlap and survives, below the 0.85 near-subset bar."""
    lines = SLOP.splitlines()
    tokens = _annotated_tokens(lines, 2)
    assert "increment" in tokens and "counter" in tokens


def test_every_finding_is_located_and_admissible(tmp_path):
    """A producer that cannot say where it is looking has not found anything -- and the admission
    gate is what enforces that, so the producer's output must survive it."""
    repo = _repo(tmp_path, {"src/widget.py": SLOP})
    for f in comment_findings(repo):
        assert f.location is not None and f.location.file and f.location.line
        assert admit_finding(f).admitted, f"producer emitted a finding the gate rejects: {f}"


def test_findings_are_advisory_never_enforce(tmp_path):
    """Producers run BEFORE blind verification, so an enforce-tier finding here would be a deletion
    nobody has corroborated."""
    repo = _repo(tmp_path, {"src/widget.py": SLOP})
    assert all(f.tier == "advise" for f in comment_findings(repo))


def test_exempt_paths_are_not_scanned(tmp_path):
    repo = _repo(tmp_path, {"src/widget.py": SLOP, "vendor/widget.py": SLOP})
    scanned = {str(p.relative_to(repo)) for p in iter_source_files(repo, exempt=["vendor"])}
    assert not any(s.startswith("vendor/") for s in scanned)


def test_machine_owned_trees_are_skipped(tmp_path):
    repo = _repo(tmp_path, {"src/w.py": SLOP, "node_modules/pkg/w.py": SLOP})
    scanned = {str(p.relative_to(repo)) for p in iter_source_files(repo)}
    assert scanned == {"src/w.py"}


def test_default_producer_matches_the_run_e1_contract(tmp_path):
    """``run_e1`` calls ``produce_findings(scope)`` -- one positional Path."""
    repo = _repo(tmp_path, {"src/widget.py": SLOP})
    assert len(default_producer()(repo)) >= 1


def test_cli_dry_run_reports_and_writes_nothing(tmp_path):
    repo = _repo(tmp_path, {"src/widget.py": SLOP})
    before = (repo / "src/widget.py").read_text()
    out = subprocess.run(
        ["python", "-m", "agent_factory.af_clean", "--repo-root", str(repo)],
        capture_output=True, text=True,
        cwd=Path(__file__).resolve().parents[1] / "src",
    )
    assert out.returncode == 0, out.stderr
    assert "admitted finding(s)" in out.stdout
    assert "dry-run" in out.stdout
    assert (repo / "src/widget.py").read_text() == before, "a dry run must change nothing"


def test_cli_refuses_a_non_git_directory(tmp_path):
    out = subprocess.run(
        ["python", "-m", "agent_factory.af_clean", "--repo-root", str(tmp_path)],
        capture_output=True, text=True,
        cwd=Path(__file__).resolve().parents[1] / "src",
    )
    assert out.returncode == 2
    assert "not a git repository" in out.stderr
