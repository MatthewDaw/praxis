"""Keep pytest from collecting eval-case *fixtures* as repo tests.

Full-pipeline cases ship a ``fixtures/`` snapshot copied into the sealed box
before the agent runs. Some fixtures are themselves Python test files (e.g. an
intentionally-failing ``test_*.py`` the agent must turn green). Those are
inputs to the harness, not tests of this repo, so they must not be collected.
"""

collect_ignore_glob = ["*/fixtures/*"]
