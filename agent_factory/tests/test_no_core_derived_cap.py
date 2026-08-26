"""building-validation check `no-core-derived-cap` (project: af-ml-research).

af-build's optional concurrency admission counts a ticket against an explicitly configured lane
(``max_cpu_parallel`` / ``max_gpu_parallel``, R15) named by its ``meta.device`` (R16) — never a
formula derived from the host's CPU core count. Wired as a pytest test (rather than a machine-authored shell one-liner)
because ``ingestion_api``'s run-body validator only allows a machine-drafted `python3 -m pytest ...`
command — this test file is that form's natural home, and it exercises the reusable scanner in
``agent_factory/tools/check_no_core_derived_cap.py`` directly, the same pattern
``test_no_github_token_leak.py`` uses for its own scanner.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.check_no_core_derived_cap import find_core_derived_expressions  # noqa: E402


def test_no_core_derived_cap() -> None:
    hits = find_core_derived_expressions()
    assert not hits, (
        "core-count-derived concurrency expression(s) found — the dispatch path must use the "
        "explicit max_cpu_parallel/max_gpu_parallel lanes (R15), never a cores-minus-N formula:\n"
        + "\n".join(hits)
    )
