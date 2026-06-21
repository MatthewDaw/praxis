"""Tests for the example deterministic checks."""

from knowledge.evals.deterministic_checks.builds import (
    function_calls,
    modifies_file,
    writes_file,
)
from knowledge.evals.eval_def import Artifact, EvalContext


def _ctx(output: str) -> EvalContext:
    return EvalContext(case_id="c", output=output)


def _ctx_art(*arts: tuple[str, str]) -> EvalContext:
    return EvalContext(case_id="c", output="", artifacts=[Artifact(path=p, status=s) for p, s in arts])


def test_function_calls_passes_when_caller_calls_callee():
    src = "def add(a, b):\n    return subtract(a, -b)\n"
    assert function_calls(_ctx(src), caller="add", callee="subtract").passed


def test_function_calls_ignores_calls_outside_the_caller():
    # subtract is defined and called elsewhere, but never inside add.
    src = (
        "def subtract(a, b):\n    return a - b\n\n"
        "def add(a, b):\n    return a + b\n\n"
        "print(subtract(10, 4))\n"
    )
    assert not function_calls(_ctx(src), caller="add", callee="subtract").passed


def test_function_calls_matches_attribute_call():
    src = "def add(a, b):\n    return calculator.subtract(a, -b)\n"
    assert function_calls(_ctx(src), caller="add", callee="subtract").passed


def test_function_calls_handles_concatenated_file_blocks():
    # The runner's multi-file output format: `# <path>\n<code>` blocks.
    output = (
        "# calculator.py\n"
        "def subtract(a, b):\n    return a - b\n\n"
        "def add(a, b):\n    return subtract(a, -b)\n\n"
        "# main.py\n"
        "from calculator import add\n"
    )
    assert function_calls(_ctx(output), caller="add", callee="subtract").passed


def test_function_calls_tolerates_unparseable_chunks():
    # A non-Python block shouldn't crash the check; the valid one still counts.
    output = (
        "# notes.md\nthis is not python ::: {\n\n"
        "# calculator.py\n"
        "def add(a, b):\n    return subtract(a, -b)\n"
    )
    assert function_calls(_ctx(output), caller="add", callee="subtract").passed


def test_writes_file_passes_only_on_created():
    assert writes_file(_ctx_art(("answer.py", "created")), path="answer.py").passed
    assert not writes_file(_ctx_art(("answer.py", "modified")), path="answer.py").passed


def test_writes_file_fails_when_absent_or_no_artifacts():
    assert not writes_file(_ctx_art(("other.py", "created")), path="answer.py").passed
    assert not writes_file(_ctx_art(), path="answer.py").passed  # single-shot: no artifacts


def test_modifies_file_passes_only_on_modified():
    assert modifies_file(_ctx_art(("calculator.py", "modified")), path="calculator.py").passed
    # strict: a freshly created file is NOT a modification
    assert not modifies_file(_ctx_art(("calculator.py", "created")), path="calculator.py").passed


def test_modifies_file_fails_when_absent_or_no_artifacts():
    assert not modifies_file(_ctx_art(("other.py", "modified")), path="calculator.py").passed
    assert not modifies_file(_ctx_art(), path="calculator.py").passed
