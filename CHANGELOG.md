# Changelog

All notable changes to the PRAXIS repository are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versioning aligns with [pyproject.toml](pyproject.toml) (`0.1.0`).

## [Unreleased]

### Added

- **`frontend-react/`** — Vite + React + TypeScript Knowledge Graph dashboard targeting [candidate-api-v1](docs/integration/candidate-api-v1.md); mock mode with 17 candidates exported from `frontend/mock_data.py`; promote/reject/contradiction resolve + eval metrics embed; `npm run build` verified.

### Changed

- **Documentation** — README, AUDIT, Monica pillar docs, wire-up guide, and CHANGELOG updated for dual Streamlit + React dashboard posture on `monica/dashboard-human-gate`.

### Fixed

- (none)

---

## [0.1.0] — 2026-06-18

Sprint Day 2 deliverables across all three pillars.

### Added

- **`knowledge/` package** — in-memory knowledge graph, prompt ingestor, whole-file graph reader, and `build_trio()` wiring factory (`feat/knowledge`).
- **Eval harness** under `knowledge/evals/` — YAML case registry, deterministic check plugins, `FakeRunner` for offline runs, and `ClaudeCodeRunner` / `ClaudeCodeJudge` for real subscription-backed evals.
- **Repo-root eval entrypoint** — `run.py` shim and `knowledge/run.py` debugger entry; `PRAXIS_EVAL_REAL=0` toggles offline mode.
- **Session capture** — Go `claude+` CLI (host / ls / stop) with PTY daemon, JSONL transcript tailer, and DynamoDB writer (`session-capture/`).
- **AWS CDK infra** — `infra/` stack provisioning the `praxis-sessions` DynamoDB table.
- **Dashboard API integration** — `ApiDataProvider` and contract v1 client with 409/400 retry logic; eval metrics embed component (`PRAXIS_EVAL_METRICS_URL`).
- **Integration contracts** — `docs/integration/candidate-api-v1.md`, `eval-metrics-v1.md`, JSON fixtures, and self-serve wire-up guide.
- **Render deploy blueprint** — `frontend/render.yaml` and deploy documentation for portfolio mock demo.
- **Repository audit** — [AUDIT.md](AUDIT.md) point-in-time health review.

### Changed

- **Repository layout** — inlined `session-capture` (dropped submodule); moved CDK from submodule to repo-root `infra/`.
- **Dashboard polish** — human-gate workflow refinements, contradiction resolution panel, confidence badges, mock/live mode banner.
- **MVP documentation** — added `docs/plans/mvp-plan.html`; parked post-MVP design under `docs/matt/future-work/`.
- **README** — updated to reflect actual code layout (`knowledge/`, `session-capture/`) instead of planned `pipeline/` / `eval/` paths.

### Fixed

- Reverted and re-landed knowledge loop after harness iteration (`Revert` + clean `feat/knowledge` merge).

---

## [0.0.2] — 2026-06-17

Sprint Day 1 — foundation, dashboard MVP, and project documentation.

### Added

- **Streamlit human-gate dashboard** — modular `frontend/` with candidate list/detail, state machine (`proposed → suggested → active`), provenance display, and mock fixtures.
- **Project documentation** — HTML project plan, pillar DRAFT plans (Matthew, Monica, Dominic), dashboard wireframes, and editable architecture SVG.
- **Team Cursor rules** — shared quality standards, dashboard patterns, pipeline/eval contracts, GitLab sync workflow.
- **Initial README** — problem statement, loop diagram, MVP scope, team roster, and sprint timeline.

### Changed

- Relocated Monica pillar docs under `docs/monica/`; added demo script and standup template.
- Dashboard architecture document aligned with implemented Streamlit module boundaries.

---

## [0.0.1] — 2026-06-17

### Added

- Initial repository commit — capstone proposal, PRD references, and team role plans.

[Unreleased]: https://labs.gauntletai.com/monicapeters/praxis/-/compare/main...HEAD
[0.1.0]: https://labs.gauntletai.com/monicapeters/praxis/-/commits/main
[0.0.2]: https://labs.gauntletai.com/monicapeters/praxis/-/commits/main
[0.0.1]: https://labs.gauntletai.com/monicapeters/praxis/-/commits/main
