"""Per-repo virtualenv setup for code cases.

Creates a venv inside the checkout and installs the project (editable, to pull
its pinned deps) plus pytest. Done at setup so the agent run stays offline.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def venv_python(dest: Path) -> Path:
    """Path to the venv's python interpreter (cross-platform)."""
    if sys.platform == "win32":
        return dest / ".venv" / "Scripts" / "python.exe"
    return dest / ".venv" / "bin" / "python"


def ensure_venv(dest: Path) -> Path:
    """Create ``.venv`` in ``dest`` and install the project + pytest; return python."""
    subprocess.run(["uv", "venv", str(dest / ".venv")], cwd=dest, capture_output=True, text=True)
    python = venv_python(dest)
    # Editable install pulls the project's own deps; pytest is the test runner.
    # Fall back to pytest-only if the project isn't pip-installable.
    install = subprocess.run(
        ["uv", "pip", "install", "--python", str(python), "-e", ".", "pytest"],
        cwd=dest,
        capture_output=True,
        text=True,
    )
    if install.returncode != 0:
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), "pytest"],
            cwd=dest,
            capture_output=True,
            text=True,
        )
    return python
