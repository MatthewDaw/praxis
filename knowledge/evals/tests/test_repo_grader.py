"""Offline tests for the real-repo grader logic (no clone, no network).

The actual RED->GREEN clone+grade is exercised by
``python -m knowledge.evals.repo.verify`` (network), not the offline suite.
"""

from knowledge.evals.deterministic_checks.behavioral import passes_target_tests
from knowledge.evals.eval_def import EvalContext
from knowledge.evals.repo import behavioral
from knowledge.evals.repo.behavioral import RunOutcome, grade
from knowledge.evals.repo.repo_task_def import RepoTask


def test_clone_url_from_owner_name_and_full_url():
    assert RepoTask(repo="a/b", base_commit="x", target_commit="y").clone_url() == "https://github.com/a/b.git"
    full = "https://github.com/a/b.git"
    assert RepoTask(repo=full, base_commit="x", target_commit="y").clone_url() == full


def test_failed_regex_parses_pytest_summary(monkeypatch):
    raw = "FAILED tests/test_more.py::ExactlyNTests::test_false - ValueError\nERROR tests/x.py::t2"
    monkeypatch.setattr(
        behavioral, "subprocess",
        type("S", (), {"run": staticmethod(lambda *a, **k: type("P", (), {"stdout": raw, "stderr": ""})())}),
    )
    res = behavioral.run_tests(python=__import__("pathlib").Path("py"), dest=__import__("pathlib").Path("."),
                               node_ids=["tests/test_more.py::ExactlyNTests::test_false"])
    assert "tests/test_more.py::ExactlyNTests::test_false" in res.failed


def test_grade_passes_when_nothing_failed(monkeypatch):
    monkeypatch.setattr(behavioral, "run_tests", lambda *a, **k: RunOutcome(failed=set(), raw=""))
    ok, ev = grade(python=None, dest=None, fail_to_pass=["a::t"], pass_to_pass=["a::u"])
    assert ok and "pass" in ev


def test_grade_fails_when_fail_to_pass_still_red(monkeypatch):
    monkeypatch.setattr(behavioral, "run_tests", lambda *a, **k: RunOutcome(failed={"a::t"}, raw=""))
    ok, ev = grade(python=None, dest=None, fail_to_pass=["a::t"], pass_to_pass=[])
    assert not ok and "FAIL_TO_PASS" in ev


def test_check_degrades_without_checkout():
    ctx = EvalContext(case_id="c", output="", checkout_path=None)
    result = passes_target_tests(ctx, fail_to_pass=["a::t"])
    assert not result.passed
    assert "no checkout" in result.evidence
