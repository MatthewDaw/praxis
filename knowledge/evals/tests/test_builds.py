"""Tests for the example deterministic checks."""

from knowledge.evals.deterministic_checks.builds import function_calls
from knowledge.evals.eval_def import EvalContext


def _ctx(output: str) -> EvalContext:
    return EvalContext(case_id="c", output=output)


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
