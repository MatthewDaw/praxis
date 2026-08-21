"""The typing/lint posture detections, and the change-class split they depend on.

The incident these encode: a checker that ABORTED reported 74 errors, the real number was 2261, and
nothing in the pipeline asked whether the tool had finished. Every abort test below therefore
asserts the aborted case is reported EVEN THOUGH its error list is short — a short list from an
aborted run is the exact shape that gets mistaken for a clean bill of health.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_factory.af_clean.findings import (
    CLASS_ANNOTATION,
    CLASS_CODE_DELETION,
    CLASS_DELETION,
    CLASS_DOCS_REWRITE,
    CLASS_MIGRATION,
    CLASS_REPORT_ONLY,
    CLASS_SPLIT,
    Finding,
    Location,
    admit_finding,
)
from agent_factory.af_clean.producers import comment_findings
from agent_factory.af_clean.typing_posture import (
    CheckerRun,
    detect_checker_abort,
    detect_missing_checker,
    detect_new_javascript,
    detect_unenforced_checker,
    typing_posture_findings,
)
from agent_factory.af_clean.verifier import (
    argv_for,
    build_verifier_payload,
    instruction_for,
    run_verifier,
)


def _git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


# --------------------------------------------------------------- 1. checker aborted early


def test_abort_marker_is_reported_even_though_the_error_count_is_small():
    """The 74-vs-2261 case. Few errors + an abort marker is the WORST case, not the best."""
    run = CheckerRun(
        tool="mypy",
        command="mypy --strict .",
        output="appeal_api/x.py:1: error: Cannot find implementation\n"
               "Found 74 errors in 9 files (errors prevented further checking)",
        exit_code=1,
        files_analysed=9,
        config_path="pyproject.toml",
    )
    finding = detect_checker_abort(run, census_file_count=133)
    assert finding is not None
    assert finding.rule == "checker-aborted-early"
    assert finding.change_class == CLASS_REPORT_ONLY
    # The marker is quoted verbatim so the reader can check the claim against the log.
    assert "errors prevented further checking" in finding.proposal
    assert "9" in finding.proposal and "133" in finding.proposal
    assert admit_finding(finding).admitted


def test_implausible_file_count_aborts_even_with_no_marker():
    run = CheckerRun(tool="tsc", command="tsc --noEmit", output="Found 0 errors.",
                     files_analysed=12, config_path="tsconfig.json")
    finding = detect_checker_abort(run, census_file_count=400)
    assert finding is not None
    assert "12" in finding.proposal


def test_complete_run_over_the_whole_census_is_not_a_finding():
    run = CheckerRun(tool="mypy", command="mypy --strict .",
                     output="Found 118 errors in 12 files", files_analysed=133,
                     config_path="pyproject.toml")
    assert detect_checker_abort(run, census_file_count=133) is None


def test_a_clean_zero_error_run_is_not_a_finding():
    run = CheckerRun(tool="mypy", command="mypy --strict .", output="Success: no issues found",
                     files_analysed=133, config_path="pyproject.toml")
    assert detect_checker_abort(run, census_file_count=133) is None


@pytest.mark.parametrize("output", [
    "error TS5083: Cannot read file 'tsconfig.json'.",
    "ERROR collecting tests/test_x.py",
    "Oops! Something went wrong! :(",
])
def test_every_ecosystems_abort_marker_is_recognised(output):
    run = CheckerRun(tool="t", command="t", output=output, files_analysed=100)
    assert detect_checker_abort(run, census_file_count=100) is not None


# --------------------------------------------------- 2. configured but not enforced / mismatched


def test_configured_checker_that_nothing_invokes_is_reported(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\nstrict = true\n")
    findings = detect_unenforced_checker(tmp_path)
    assert [f.rule for f in findings] == ["checker-configured-but-not-enforced"]
    assert findings[0].location.file == "pyproject.toml"
    assert admit_finding(findings[0]).admitted


def test_configured_and_invoked_checker_is_not_reported(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\nstrict = true\n")
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("jobs:\n  types:\n    run: mypy --strict .\n")
    assert detect_unenforced_checker(tmp_path) == []


def test_documented_gate_command_diverging_from_the_enforced_one_is_reported(tmp_path):
    """The subtler variant, and it cost a real build round: pyproject documented `mypy appeal_api`
    while CI ran `mypy --strict .`, so the number people quoted and the number that blocked a build
    were measuring different trees."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.mypy]\n# gate command: mypy appeal_api\nstrict = true\n")
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("    run: mypy --strict .\n")
    findings = detect_unenforced_checker(tmp_path)
    assert [f.rule for f in findings] == ["checker-gate-command-mismatch"]
    assert "mypy appeal_api" in findings[0].proposal
    assert "mypy --strict ." in findings[0].proposal


# ------------------------------------------------------------- 3. no checker configured at all


def test_python_project_with_no_checkers_reports_both_gates(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    rules = {(f.rule, f.location.file) for f in detect_missing_checker(tmp_path)}
    assert rules == {("no-checker-configured", "pyproject.toml")}
    assert len(detect_missing_checker(tmp_path)) == 2  # one for the type gate, one for the linter


def test_a_configured_gate_suppresses_its_own_missing_finding(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\n[tool.ruff]\n")
    assert detect_missing_checker(tmp_path) == []


def test_no_manifest_means_no_ecosystem_and_no_finding(tmp_path):
    assert detect_missing_checker(tmp_path) == []


# -------------------------------------------------------------------- 4. the JavaScript position


def test_new_js_file_in_a_typescript_repo_is_reported(tmp_path):
    (tmp_path / "tsconfig.json").write_text("{}")
    findings = detect_new_javascript(tmp_path, ["src/widget.js"])
    assert [f.rule for f in findings] == ["new-javascript-in-typescript-repo"]
    assert findings[0].location.file == "src/widget.js"
    assert findings[0].change_class == CLASS_REPORT_ONLY


def test_a_repo_with_no_typescript_gets_nothing(tmp_path):
    """Introducing a TS toolchain is a project decision, exactly like turning on a checker."""
    assert detect_new_javascript(tmp_path, ["src/widget.js"]) == []


def test_config_files_a_tool_requires_as_js_are_carved_out(tmp_path):
    (tmp_path / "tsconfig.json").write_text("{}")
    assert detect_new_javascript(tmp_path, ["tailwind.config.js", "eslint.config.js"]) == []


def test_exempt_trees_are_not_reported(tmp_path):
    (tmp_path / "tsconfig.json").write_text("{}")
    assert detect_new_javascript(tmp_path, ["vendor/lib/a.js"], exempt=["vendor"]) == []


def test_existing_js_files_are_not_swept_in(tmp_path):
    """Only ADDED paths are enforced — a bulk .js -> .ts conversion is a migration, not a cleanup."""
    (tmp_path / "tsconfig.json").write_text("{}")
    (tmp_path / "legacy.js").write_text("var x = 1\n")
    assert detect_new_javascript(tmp_path, []) == []


# ------------------------------------------------------------------------- the composed producer


def test_typing_posture_findings_are_all_report_only_and_all_admitted(tmp_path):
    _git_repo(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    findings = typing_posture_findings(
        tmp_path,
        checker_runs=[CheckerRun(tool="mypy", command="mypy .", output="errors prevented "
                                 "further checking", files_analysed=1, config_path="pyproject.toml")],
        census_file_count=50,
    )
    assert findings, "a repo with no checkers and an aborted run must produce findings"
    assert all(f.change_class == CLASS_REPORT_ONLY for f in findings)
    assert all(admit_finding(f).admitted for f in findings), "posture findings must survive the gate"


def test_no_checker_run_means_no_abort_claim(tmp_path):
    """"No run" and "an aborted run" are different claims, and only one of them is evidence."""
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\n[tool.ruff]\n")
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("run: mypy .\nrun: ruff check .\n")
    assert typing_posture_findings(tmp_path, checker_runs=(), census_file_count=50) == []


# --------------------------------------------------------------- the admission gate's class rules


def test_a_report_only_finding_needs_no_slop_pole():
    f = Finding(rule="r", tier="advise", location=Location("a.py", 1),
                change_class=CLASS_REPORT_ONLY)
    assert admit_finding(f).admitted


def test_a_structural_split_is_admitted_without_a_slop_pole():
    f = Finding(rule="module-split", tier="enforce", location=Location("large.py", 1),
                change_class=CLASS_SPLIT)
    assert admit_finding(f).admitted


def test_a_state_migration_is_admitted_without_a_slop_pole():
    f = Finding(rule="canonical-store-migration", tier="enforce",
                location=Location("legacy.py", 1), change_class=CLASS_MIGRATION)
    assert admit_finding(f).admitted


def test_a_deletion_still_requires_a_pole():
    f = Finding(rule="r", tier="advise", location=Location("a.py", 1))
    assert f.change_class == CLASS_DELETION            # the pre-split default is preserved
    assert not admit_finding(f).admitted


def test_an_unknown_change_class_is_dropped():
    f = Finding(rule="r", tier="advise", location=Location("a.py", 1),
                change_class="rewrite-everything", pole="bloat")
    verdict = admit_finding(f)
    assert not verdict.admitted
    assert "no verifier question exists" in verdict.reason


def test_an_unpoled_class_may_not_declare_a_bogus_pole():
    f = Finding(rule="r", tier="advise", location=Location("a.py", 1),
                change_class=CLASS_ANNOTATION, pole="sideways")
    assert not admit_finding(f).admitted


# ------------------------------------------------------------------- the verifier question split


def test_every_change_class_asks_its_own_question():
    questions = {
        cls: instruction_for(cls)
        for cls in (
            "deletion", "code-deletion", "consolidation", "split", "migration", "docs-rewrite",
            "annotation", "lint-fix",
            "js-to-ts", "test-graduation",
        )
    }
    assert len(set(questions.values())) == len(questions), "a shared question verifies nothing"


def test_the_annotation_question_judges_correctness_not_acceptance():
    """A wrong-but-accepted annotation is the dangerous case: `x: Any` and `x: str` both satisfy the
    checker, and only one is true. A verifier grading acceptance would reward the exact escape-hatch
    behaviour the axis punishes."""
    text = instruction_for("annotation").lower()
    assert "correctness" in text
    assert "any" in text and "ignore" in text
    assert "behaviour-neutral" in text


def test_test_graduation_question_requires_real_focused_and_broader_witnesses():
    text = instruction_for("test-graduation").lower()
    for required in (
        "expected-failure marker", "no test body", "focused witnesses", "without xpass",
        "collection", "test was weakened", "environmental absence", "observable",
    ):
        assert required in text


def test_docs_rewrite_question_judges_semantic_and_operational_completeness():
    text = instruction_for(CLASS_DOCS_REWRITE).lower()
    assert "factual" in text
    assert "authorization" in text
    assert "broken references" in text
    assert "omitted invariant" in text


def test_code_deletion_fails_closed_on_reachability_and_compatibility_obligations():
    text = instruction_for(CLASS_CODE_DELETION).lower()
    for required in (
        "located reachability proof", "unreachable", "preserved caller", "observable behavior",
        "compatibility", "migration", "tier-2 witnesses", "dynamic", "bounded",
    ):
        assert required in text


def test_consolidation_asks_about_erased_divergence_between_the_duplicates():
    assert "diverg" in instruction_for("consolidation").lower()


def test_split_question_pins_every_observable_cli_and_import_surface():
    text = instruction_for("split").lower()
    for required in (
        "public import path", "entry point", "argument/default", "help byte", "stdout/stderr",
        "exit code", "validation and error order", "side effect", "persistence", "import cycle",
        "semantic change",
    ):
        assert required in text


def test_migration_question_pins_losslessness_and_single_authority():
    text = instruction_for("migration").lower()
    for required in (
        "exactly once", "invented provenance", "identity drift", "reconstructable", "byte",
        "crash/restart", "idempotent", "source of truth", "reversible",
    ):
        assert required in text


def test_report_only_has_no_question_and_refuses_to_borrow_the_deletion_one():
    with pytest.raises(ValueError):
        instruction_for("report-only")
    with pytest.raises(ValueError):
        instruction_for("nonexistent-class")


def test_the_default_payload_is_still_the_deletion_question():
    """Byte-compatibility with every pre-split caller: omitting the class asks what it always did."""
    default = build_verifier_payload("diff", "/repo")
    explicit = build_verifier_payload("diff", "/repo", change_class="deletion")
    assert default.argv == explicit.argv


def test_the_class_reaches_the_subprocess_only_as_a_question(monkeypatch):
    """B23 still holds: the caller SELECTS a class, it never writes the prompt, and nothing about
    how the diff was produced can ride along."""
    payload = build_verifier_payload("d", "/repo", change_class="lint-fix")
    assert payload.argv == argv_for("lint-fix")
    assert payload.stdin_payload() == {"diff": "d", "repo_path": "/repo"}


def test_run_verifier_passes_the_class_through():
    seen: dict = {}

    class _Result:
        stdout = json.dumps({"endorsed_hunk_ids": ["h1"]})

    def runner(argv, **kwargs):
        seen["argv"] = argv
        return _Result()

    verdict = run_verifier("d", "/repo", change_class="js-to-ts", runner=runner,
                           transcript="SECRET", rationale="SECRET")
    assert verdict.endorsed_hunk_ids == frozenset({"h1"})
    assert seen["argv"][-1] == instruction_for("js-to-ts")
    assert "SECRET" not in " ".join(seen["argv"])


# ------------------------------------------- false positives found by running af-clean on praxis
#
# A real dry run produced 18 findings, of which 11 were wrong in two distinct ways. Both are pinned
# here because both would silently return the moment the detector is touched.


def test_a_section_divider_is_protected_not_eligible():
    """`# --- orgs ---...` scored a perfect overlap: a banner's label names the section beneath it,
    which is the POINT of a banner, not a restatement of it."""
    from agent_factory.af_clean_comment_triage import classify_comment

    verdict = classify_comment(
        "--- orgs --------------------------------------------------------------",
        {"orgs"},
    )
    assert verdict.verdict == "protected"


def test_a_bare_rule_line_is_protected():
    from agent_factory.af_clean_comment_triage import classify_comment

    assert classify_comment("=" * 70, set()).verdict == "protected"
    assert classify_comment("#" * 40 + " main", {"main"}).verdict == "protected"


def test_a_short_dash_is_not_mistaken_for_a_divider():
    """Three chars is a hyphenated phrase or an em-dash, not a rule. Only 4+ repeats are layout."""
    from agent_factory.af_clean_comment_triage import classify_comment

    assert classify_comment("increment counter", {"increment", "counter"}).verdict == "eligible"


def test_a_hash_inside_a_string_literal_is_not_a_comment(tmp_path):
    """af-clean proposed deleting the comments inside its OWN test fixtures — the `SLOP = '''...'''`
    constants written to look like slop so the detector can be tested against them."""
    src = tmp_path / "test_fixture.py"
    src.write_text(
        "SLOP = '''class Widget:\n"
        "    def increment_counter(self, counter):\n"
        "        # increment counter\n"
        "        counter = counter + 1\n"
        "'''\n"
    )
    assert comment_findings(tmp_path) == []


def test_a_real_comment_in_the_same_file_is_still_found(tmp_path):
    """The tokenizer must not silence the detector — only the string-literal false positives."""
    src = tmp_path / "widget.py"
    src.write_text(
        "SLOP = '''# increment counter'''\n\n"
        "def increment_counter(self, counter):\n"
        "    # increment counter\n"
        "    return counter + 1\n"
    )
    findings = comment_findings(tmp_path)
    assert [f.location.line for f in findings] == [4], "only the real comment, at its real line"


def test_a_file_that_will_not_tokenize_falls_back_to_the_line_scan(tmp_path):
    """A syntax error must not mean 'this file has no comments' — that would silently shrink the
    detector's reach on exactly the files most likely to be a mess."""
    src = tmp_path / "broken.py"
    src.write_text("def increment_counter(self, counter):\n    # increment counter\n    ((((\n")
    assert [f.location.line for f in comment_findings(tmp_path)] == [2]


# ------------------------------------------------- a validation command that cannot even be spawned


def test_an_unspawnable_command_fails_the_phase_instead_of_killing_the_run():
    """`python -m agent_factory.af_clean` with no flags — the documented default — died on an
    unhandled FileNotFoundError when discovery produced a `python` command on a box that has only
    `python3`. It must be a FAILED phase, never SKIPPED: 'did not run' and 'passed' must not be the
    same observable outcome."""
    from agent_factory.af_clean_validate import FAILED, run_validation_and_remediation

    def _missing_binary(argv, cwd):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    report = run_validation_and_remediation(
        ".", runner=_missing_binary, commands={"test": "python -m pytest"}, iteration_cap=0)
    phase = next(p for p in report.phases if p.name == "full_test_suite")
    assert phase.status == FAILED
    assert "DID NOT RUN" in phase.detail
    assert report.overall_status != "passed"
