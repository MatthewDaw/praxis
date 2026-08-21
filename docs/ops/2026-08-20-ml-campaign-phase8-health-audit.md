# ML campaign foundation Phase 8 health audit

Audited without training, seeding, live-registry writes, or live-state mutation:

- `sports_analysis@cf48586eb16ee63aa405a574c92f798fb28e91df`
- `praxis@dea754c895079976e35d9978f3c67844b76439b2`

## Passing gates

| Gate | Command | Result |
|---|---|---|
| Sports collection | `make check-collect` | 2,965 collected; zero errors |
| Praxis registry | `PRAXIS_DB_DISABLED=1 uv run pytest knowledge/ml_registry/tests -q -p no:cacheprovider` | 938 passed; one warning |
| Registry schema | create a temporary `knowledge.ml_registry.storage.Registry`, then query `sqlite_master` | exactly `aliases`, `artifacts`, `events`, `experiments`, `lineage`, `model_versions`, `registered_models`, `runs` |
| Process cleanup | `ps -axo pid,ppid,pgid,stat,command` filtered for the audit worktrees and campaign fixtures | no owned fixture/controller process remained |

## Remaining acceptance failures

| Requirement | Located failure | Reproduction |
|---|---|---|
| One discoverable validation target per repository | Sports `Makefile` has no campaign-foundation target; Praxis has no target that sets `PRAXIS_DB_DISABLED=1` | `rg -n '^[-a-zA-Z0-9_]+:' Makefile pyproject.toml` in each repository |
| Phase 8 sports integration/production/archive gates | `tests/integration/test_fixture_m_arm_lifecycle.py`, `tests/experimentation/campaigns/test_a01_detection_migration.py`, `tests/experimentation/harness/test_contracts.py`, `tests/archive/test_live_state_freeze.py` | `PRAXIS_REPO_ROOT=<praxis> uv run python -m pytest -q tests/integration tests/experimentation tests/production tests/archive` gives 5 failed, 150 passed, 7 xfailed |
| Fixture M arm lifecycle | `src/assoc_lab/arms.py:KNOWN_TOGGLES`, `src/contact_lab/train.py:ARMS`, and `src/det_lab/campaign.py:ARMS` remain; no `runs/*` tags exist | `uv run pytest -q tests/integration/test_fixture_m_arm_lifecycle.py` |
| Canonical controller completion | A01 detection's real campaign-job proof remains `waiting` instead of `complete` | `uv run pytest -q tests/experimentation/campaigns/test_a01_detection_migration.py::test_real_campaign_job_dispatches_and_observes_finalize_only` with `PRAXIS_REPO_ROOT=<praxis>` |
| Campaign-neutral harness | `src/sports_analysis/experimentation/harness/compare.py` contains a forbidden lifecycle token | `uv run pytest -q tests/experimentation/harness/test_contracts.py::test_harness_has_no_registry_or_lifecycle_policy` |
| Archive integrity in a clean checkout | `tests/archive/test_live_state_freeze.py` requires a live stale lock that is absent in the detached checkout | `uv run pytest -q tests/archive/test_live_state_freeze.py::test_stale_locks_are_preserved_as_evidence_not_discarded` |
| Praxis genericity | `knowledge/ml_registry/preflight.py` still embeds sports paths, campaign names, and model IDs | `rg -n -i 'sports_analysis|detection_shipped|contact_point|court_marking|model-[0-9a-f]{12}' knowledge/ml_registry/preflight.py` |
| Legacy control-plane retirement | Six scripts remain: `af-ml-campaign-loop.sh`, `af-ml-campaign-queue.sh`, `af-ml-agent-queue.sh`, `af-ml-supervise-keepalive.sh`, `launch-codex-dual-campaigns.sh`, `af-ml-campaign-preflight.sh` | `test -e agent_factory/scripts/<name>` |
| One public CLI namespace | The operator workflow is exposed through `knowledge.ml_registry.controller_cli`, while `python -m knowledge.ml_registry.cli portfolio --help` is rejected | run both help commands |
| Repository lint health | Sports has 694 Ruff findings; Praxis had five bounded findings at the audited commit | `uvx ruff@0.15.20 check .` (sports); `uv run ruff check .` (Praxis) |
| Single sports namespace and size ceiling | `pyproject.toml` still packages eight top-level namespaces; tracked Python is 227,280 lines, above 150,000 | `rg -n '^packages' pyproject.toml`; `git ls-files '*.py' | xargs wc -l | tail -1` |

The `/af-clean` deletion/consolidation batches and the full-manifest no-training rehearsal remain
unmet. No deletion is proposed by this report.
