"""The E1 human path: producers + the ``python -m agent_factory.af_clean`` entry point.

These cover the gap that shipped with the original build set: the engine and the detectors both
existed and were tested, but nothing connected a typed command to them, so every one of the bugs
below was invisible to a green suite -- a manifest method iterated as if it were a list, a verdict
vocabulary that never matched, and comments judged without the signature they restate.
"""

from __future__ import annotations

import json as _json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from agent_factory.af_clean.applier import apply_findings
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


# --------------------------------------------------------------- the apply path (gate → verify → commit)


def _endorsing_verifier(argv, **kwargs):
    """A verifier that endorses every hunk it is shown, with a DISTINCT id per hunk — which is what
    a real verdict looks like, and what makes 'endorsed everything' distinguishable from 'endorsed
    one hunk twice'."""
    diff = kwargs.get("input") or ""
    n = sum(1 for ln in diff.splitlines() if ln.startswith("@@")) or 1
    ids = [f"h{i}" for i in range(1, n + 1)]
    return SimpleNamespace(stdout=_json.dumps({"endorsed_hunk_ids": ids, "verdict": "endorse"}))


def _refusing_verifier(argv, **kwargs):
    """A verifier that endorses nothing — the case that must leave no trace."""
    return SimpleNamespace(stdout=_json.dumps({"endorsed_hunk_ids": [], "verdict": "refuse"}))


def test_apply_removes_slop_and_keeps_the_why_comment(tmp_path):
    repo = _repo(tmp_path, {"src/widget.py": SLOP})
    out = apply_findings(repo, comment_findings(repo), verifier_runner=_endorsing_verifier)
    text = (repo / "src/widget.py").read_text()
    assert "# increment counter" not in text
    assert "INC-441" in text, "a comment carrying a reason must survive the applier"
    assert "counter = counter + 1" in text, "code must be untouched"
    assert len(out.applied) >= 1


def test_verifier_refusal_leaves_no_trace(tmp_path):
    """An unendorsed edit must be fully restored: a tree still carrying rejected changes is how a
    refusal becomes an accidental commit later."""
    repo = _repo(tmp_path, {"src/widget.py": SLOP})
    before = (repo / "src/widget.py").read_text()
    out = apply_findings(repo, comment_findings(repo), verifier_runner=_refusing_verifier)
    assert (repo / "src/widget.py").read_text() == before
    assert out.applied == [] and out.verifier_rejected


def test_advise_only_findings_are_reported_not_applied(tmp_path):
    """The gate, not the applier, decides — an advise-tier rule can never delete."""
    from agent_factory.af_clean.findings import Finding, Location
    repo = _repo(tmp_path, {"src/widget.py": SLOP})
    advisory = Finding(rule="some-judgment-call", tier="advise",
                       location=Location(file="src/widget.py", line=3), pole="bloat")
    out = apply_findings(repo, [advisory], verifier_runner=_endorsing_verifier)
    assert out.applied == [] and out.reported


def test_scar_findings_are_never_applied(tmp_path):
    """Defensive code with a bug-fix commit behind it is load-bearing; the gate refuses it
    unconditionally, regardless of tier."""
    from agent_factory.af_clean.findings import Finding, Location
    repo = _repo(tmp_path, {"src/widget.py": SLOP})
    scar = Finding(rule="defensive-code-is-a-scar", tier="enforce",
                   location=Location(file="src/widget.py", line=5), pole="bloat")
    out = apply_findings(repo, [scar], verifier_runner=_endorsing_verifier)
    assert out.applied == []


def test_stale_line_number_is_refused(tmp_path):
    """Deleting by a line number that no longer holds a comment is how a cleaner corrupts a repo."""
    from agent_factory.af_clean.findings import Finding, Location
    repo = _repo(tmp_path, {"src/widget.py": SLOP})
    stale = Finding(rule="comment-no-information-gain", tier="advise",
                    location=Location(file="src/widget.py", line=4), pole="bloat")  # code, not comment
    out = apply_findings(repo, [stale], verifier_runner=_endorsing_verifier)
    assert out.applied == [] and "counter = counter + 1" in (repo / "src/widget.py").read_text()


def test_nothing_is_written_before_verification(tmp_path):
    """The window that mattered: a run killed mid-verification must not leave a half-cleaned repo.
    The verifier judges a PROPOSAL, so the tree is untouched until it endorses."""
    repo = _repo(tmp_path, {"src/widget.py": SLOP})
    before = (repo / "src/widget.py").read_text()
    seen: dict = {}

    def _verifier_that_inspects_the_tree(argv, **kwargs):
        seen["tree_at_verify_time"] = (repo / "src/widget.py").read_text()
        return _refusing_verifier(argv, **kwargs)

    apply_findings(repo, comment_findings(repo),
                   verifier_runner=_verifier_that_inspects_the_tree)
    assert seen["tree_at_verify_time"] == before, "tree was edited before the verifier ruled"
    assert (repo / "src/widget.py").read_text() == before


def test_multiple_findings_in_one_file_compose(tmp_path):
    """Two removals in one file must compose, not be computed against stale disk text."""
    repo = _repo(tmp_path, {"src/widget.py": SLOP + '''
    def set_name(self, name):
        # set the name
        self.name = name
'''})
    out = apply_findings(repo, comment_findings(repo), verifier_runner=_endorsing_verifier)
    text = (repo / "src/widget.py").read_text()
    assert "# increment counter" not in text and "# set the name" not in text
    assert len(out.applied) == 2
    assert "INC-441" in text and "self.name = name" in text


def test_default_runner_turns_a_hanging_verifier_into_no_endorsement():
    """A verifier that never answers has not endorsed anything. An observed run sat past nine
    minutes, so the subprocess is bounded -- and a timeout must read as silence, never as assent."""
    from agent_factory.af_clean.applier import default_subprocess_runner

    result = default_subprocess_runner(["sleep", "5"], timeout=0.2, capture_output=True, text=True)
    assert result.stdout == "", "a timed-out verifier must produce no endorsement"


def test_silent_verifier_applies_nothing(tmp_path):
    """Empty verifier output endorses nothing, so the tree stays exactly as it was."""
    from types import SimpleNamespace as _NS

    repo = _repo(tmp_path, {"src/widget.py": SLOP})
    before = (repo / "src/widget.py").read_text()
    out = apply_findings(repo, comment_findings(repo),
                         verifier_runner=lambda argv, **kw: _NS(stdout=""))
    assert out.applied == []
    assert (repo / "src/widget.py").read_text() == before


def test_endorsement_is_per_file_and_a_refusal_does_not_spread(tmp_path):
    """The verifier answers with hunk ids that cannot be mapped back to files, so each file's patch
    is verified on its own. Refusing one file must not block the other, and endorsing one must not
    approve its neighbour."""
    from types import SimpleNamespace as _NS
    import json as _j

    repo = _repo(tmp_path, {"src/keep.py": SLOP, "src/drop.py": SLOP})
    keep_before = (repo / "src/keep.py").read_text()

    def _endorses_only_drop(argv, **kw):
        diff = kw.get("input") or ""
        ok = "drop.py" in diff
        return _NS(stdout=_j.dumps({"endorsed_hunk_ids": ["h1"] if ok else [],
                                    "verdict": "endorse" if ok else "refuse"}))

    out = apply_findings(repo, comment_findings(repo), verifier_runner=_endorses_only_drop)
    assert "# increment counter" not in (repo / "src/drop.py").read_text(), "endorsed file must apply"
    assert (repo / "src/keep.py").read_text() == keep_before, "refused file must be untouched"
    assert out.applied and out.verifier_rejected


def test_refusing_one_finding_in_a_file_keeps_the_others(tmp_path):
    """The case neither the batch nor the per-file design could express: two removals in ONE file,
    one endorsed and one refused. Verification is per finding, so a refusal removes exactly its own
    change and nothing else -- no 'partial endorsement' to interpret."""
    from types import SimpleNamespace as _NS
    import json as _j

    repo = _repo(tmp_path, {"src/widget.py": SLOP + '''
    def set_name(self, name):
        # set the name
        self.name = name
'''})

    def _refuses_only_the_set_name_removal(argv, **kw):
        diff = kw.get("input") or ""
        refuse = "set the name" in diff
        return _NS(stdout=_j.dumps({"endorsed_hunk_ids": [] if refuse else ["h1"]}))

    out = apply_findings(repo, comment_findings(repo),
                         verifier_runner=_refuses_only_the_set_name_removal)
    text = (repo / "src/widget.py").read_text()
    assert "# increment counter" not in text, "the endorsed removal must land"
    assert "# set the name" in text, "the refused removal must survive untouched"
    assert len(out.applied) == 1 and len(out.verifier_rejected) == 1
    assert "INC-441" in text and "self.name = name" in text


def test_each_finding_gets_its_own_verdict(tmp_path):
    """One change per verification is what makes partial endorsement impossible by construction."""
    repo = _repo(tmp_path, {"src/widget.py": SLOP + '''
    def set_name(self, name):
        # set the name
        self.name = name
'''})
    calls: list[str] = []

    def _counting(argv, **kw):
        calls.append(kw.get("input") or "")
        return _endorsing_verifier(argv, **kw)

    apply_findings(repo, comment_findings(repo), verifier_runner=_counting)
    assert len(calls) == 2, "each finding must be verified on its own"
    for payload in calls:
        # A hunk header is "@@ -1,6 +1,5 @@" -- two "@@" per hunk -- so count hunk STARTS.
        assert payload.count("@@ -") == 1, "a verification must cover exactly one change"


def test_applied_work_is_committed(tmp_path):
    """The commit stack must actually produce a commit. It silently did not, because argv from
    apply_commit_stack already starts with "git" and the runner prepended a second one."""
    repo = _repo(tmp_path, {"src/widget.py": SLOP})
    head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                 capture_output=True, text=True).stdout.strip()
    apply_findings(repo, comment_findings(repo), verifier_runner=_endorsing_verifier)
    head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                capture_output=True, text=True).stdout.strip()
    assert head_after != head_before, "endorsed work must land as a commit"
    log = subprocess.run(["git", "log", "--oneline", "-1"], cwd=repo,
                         capture_output=True, text=True).stdout
    assert "af-clean" in log, f"commit should name af-clean, got: {log}"
