"""Resolve the hook modules however the interpreter was launched.

``hooks/`` is a plain directory, not an installed package, so ``from hooks import _praxis``
only resolves when the directory that *contains* ``hooks/`` is on ``sys.path``. That is true
under pytest (``pythonpath = ["src", ".", "hooks"]``) and when the cwd happens to be
``agent_factory/`` -- and false in the one place that matters most: ``af-ticket-loop.sh``
exports ``PYTHONPATH=<root>/hooks:<root>/src``, putting the hook *modules* on the path but not
their parent. The 2026-08-07 build shipped a whole subsystem behind that asymmetry: 1207 unit
tests green, while the loop's only call into it (``python -m agent_factory.af_retro --flags``)
died on ``ModuleNotFoundError: No module named 'hooks'`` and was swallowed by ``|| true``.

Importing through this module removes the dependency on how the process was started: the
directory containing ``hooks/`` is derived from this file's own location and put on the path
before the import. Two layouts are supported, and only the one that actually exists on disk is
added:

* source checkout -- ``<root>/src/agent_factory/_hooks.py`` with the hooks at ``<root>/hooks``
  (``parents[2]``);
* installed wheel -- ``<site-packages>/agent_factory/_hooks.py`` with the hooks shipped as a
  sibling top-level ``<site-packages>/hooks`` (``parents[1]``); see the
  ``[tool.hatch.build.targets.wheel]`` ``packages`` list in ``pyproject.toml``.

Everything resolves to ONE module object per file, never a mix of ``hooks._praxis`` and a bare
top-level ``_praxis`` -- two objects for one file silently break every test (and every runtime
consumer) that monkeypatches ``_praxis`` on one of them. That was not merely a risk: importing
this seam from a plain interpreter really did execute ``hooks/_praxis.py`` twice
(``hooks._ticket_state._praxis is agent_factory.ingestion_api._praxis`` was False), because
``hooks/_ticket_state.py`` reached its sibling by the bare name while this module reached it by
the dotted one.

The single object is minted in ``hooks/_ticket_state._canonical_module``, which publishes it under
BOTH import names -- so a bare hook subprocess (``import _praxis``) and a library consumer
(``from hooks import _praxis``) converge. This module therefore takes ``_praxis`` OFF the
already-canonicalized ``_ticket_state`` rather than importing it a second way: identity by
construction, not by convention.

Paths are APPENDED, never inserted at position 0: the containing directory also holds ``tests/``,
``scripts/`` and ``evals/``, and shadowing an installed distribution with those from the front of
the path is a strictly worse failure than the one being fixed.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
# parents[2] -> source checkout root (.../agent_factory); parents[1] -> site-packages when installed.
for _root in (_HERE.parents[2], _HERE.parents[1]):
    if (_root / "hooks").is_dir() and str(_root) not in sys.path:
        sys.path.append(str(_root))

from hooks import _ticket_state  # noqa: E402

# NOT `from hooks import _praxis`: that is the second import route whose independence forked the
# module. _ticket_state has already resolved the one canonical object and registered it under both
# names; take it from there so the two can never diverge again.
_praxis = _ticket_state._praxis

__all__ = ["_praxis", "_ticket_state"]
