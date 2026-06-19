"""Tests for fixture mounting: load_case populates source_dir and the copy
helper places fixture files into the sealed box (structure preserved)."""

from pathlib import Path

import yaml

from knowledge.evals.claude_code import mount_fixtures
from knowledge.evals.eval_def import EvalCase
from knowledge.evals.run import load_case


def _write_case(case_dir: Path) -> None:
    case_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "id": case_dir.name,
        "component": "knowledge_graph",
        "deterministic_checks": [
            {"name": "ne", "ref": "knowledge.evals.deterministic_checks.builds:output_nonempty"}
        ],
    }
    (case_dir / "case.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def test_load_case_populates_source_dir(tmp_path):
    case_dir = tmp_path / "mycase"
    _write_case(case_dir)
    case = load_case(case_dir)
    assert case.source_dir == str(case_dir)


def test_mount_fixtures_copies_tree(tmp_path):
    case_dir = tmp_path / "mycase"
    _write_case(case_dir)
    fixtures = case_dir / "fixtures"
    (fixtures / "sub").mkdir(parents=True)
    (fixtures / "top.txt").write_text("hello", encoding="utf-8")
    (fixtures / "sub" / "deep.py").write_text("x = 1", encoding="utf-8")

    case = load_case(case_dir)
    box = tmp_path / "box"
    box.mkdir()
    copied = mount_fixtures(case, box)

    assert copied == 2
    assert (box / "top.txt").read_text(encoding="utf-8") == "hello"
    assert (box / "sub" / "deep.py").read_text(encoding="utf-8") == "x = 1"


def test_mount_fixtures_noop_without_source_dir(tmp_path):
    case = EvalCase(
        id="x",
        component="knowledge_graph",
        deterministic_checks=[
            {"name": "ne", "ref": "knowledge.evals.deterministic_checks.builds:output_nonempty"}
        ],
    )
    assert mount_fixtures(case, tmp_path) == 0


def test_mount_fixtures_noop_without_fixtures_dir(tmp_path):
    case_dir = tmp_path / "mycase"
    _write_case(case_dir)
    case = load_case(case_dir)
    box = tmp_path / "box"
    box.mkdir()
    assert mount_fixtures(case, box) == 0
