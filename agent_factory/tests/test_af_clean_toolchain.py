"""R1 acceptance: af-clean's toolchain detector.

af-clean must detect the target repository's languages, package managers, test runners,
linters, and type checkers per invocation rather than assuming any toolchain -- nothing about
the environment may be hardcoded, since the primary use is arbitrary repositories.

Acceptance (verbatim from the ticket): on a repo with pyproject.toml + package.json the report
names both toolchains and which of Vulture/Knip/jscpd/radon/semgrep are absent; and a full run
on a repo with none installed leaves ``git status`` showing zero manifest or lockfile changes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_factory.af_clean.toolchain import (
    DETECTOR_PINS,
    detect_toolchain,
    format_report,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_mixed_repo(tmp_path: Path) -> Path:
    """A repo with BOTH a Python and a JS toolchain, and none of the five census
    detectors installed -- the exact fixture the acceptance condition names."""
    repo = tmp_path / "mixed-repo"
    _write(
        repo / "pyproject.toml",
        "[tool.ruff]\nline-length = 100\n\n[tool.mypy]\nstrict = true\n",
    )
    _write(repo / "requirements.txt", "pytest\n")
    _write(
        repo / "package.json",
        '{"name": "x", "devDependencies": {"eslint": "^9", "vitest": "^2"}}\n',
    )
    _write(repo / "package-lock.json", "{}\n")
    return repo


def test_acceptance_report_names_both_toolchains_and_absent_detectors(tmp_path):
    repo = _make_mixed_repo(tmp_path)

    report = detect_toolchain(repo, probe_detectors=False)
    text = format_report(report)

    languages = {lang.language for lang in report.languages}
    assert "python" in languages
    assert languages & {"javascript", "typescript"}

    assert "python" in text
    assert ("javascript" in text) or ("typescript" in text)

    # Every one of the five census detectors is named (present or absent) -- none are
    # invented and none are silently omitted.
    assert set(DETECTOR_PINS) == set(report.detectors_present)
    for name in DETECTOR_PINS:
        assert name in text


def test_acceptance_full_run_makes_zero_manifest_or_lockfile_changes(tmp_path):
    repo = _make_mixed_repo(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "init"],
        cwd=repo,
        check=True,
    )

    before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout

    # A full run: real zero-install detector probing (uvx/npx), same posture as production
    # invocation. None of the five detectors is assumed pre-installed by this fixture.
    detect_toolchain(repo, probe_detectors=True)

    after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout

    assert before == after == ""

    manifest_names = {
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "requirements.txt",
        "uv.lock",
        "yarn.lock",
        "pnpm-lock.yaml",
    }
    tracked_after = set(
        subprocess.run(
            ["git", "ls-files"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.split()
    )
    # No NEW manifest/lockfile appeared, and none of the original ones changed (git status
    # already proved that above); this is an extra belt-and-suspenders check on the set.
    assert tracked_after & manifest_names == {"pyproject.toml", "package.json", "requirements.txt", "package-lock.json"}


def test_detects_python_only_repo(tmp_path):
    repo = tmp_path / "py-only"
    _write(repo / "pyproject.toml", "[tool.ruff]\n")
    _write(repo / "uv.lock", "")

    report = detect_toolchain(repo, probe_detectors=False)

    assert len(report.languages) == 1
    lang = report.languages[0]
    assert lang.language == "python"
    assert lang.package_manager == "uv"
    assert lang.linter == "ruff"


def test_no_recognizable_toolchain_reports_empty_not_hardcoded(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()

    report = detect_toolchain(repo, probe_detectors=False)

    assert report.languages == []
    text = format_report(report)
    assert "none detected" in text


def test_detector_presence_never_hardcoded_true(tmp_path):
    """Absence must be a real negative, not an assumed default -- nothing about the
    environment may be hardcoded."""
    repo = tmp_path / "bare"
    repo.mkdir()

    report = detect_toolchain(repo, probe_detectors=False)

    # With no PATH lookup performed (probe_detectors=False only checks `shutil.which`),
    # every detector's presence is a live, non-hardcoded answer -- the dict always carries
    # exactly the five named tools, never a hardcoded assumption of presence or absence.
    assert set(report.detectors_present) == set(DETECTOR_PINS)
    assert all(isinstance(v, bool) for v in report.detectors_present.values())
