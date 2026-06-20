# PRAXIS Plan Alignment Gap Checklist — Monica (Scrum Master + Dashboard)

**Owner:** Monica Peters — Dashboard & Human Gate Lead · **Daily Scrum Master**  
**Generated:** 2026-06-18 · **Updated at:** each daily sync  
**Source of truth:** [PRAXIS_Project_Plan.html](../plans/PRAXIS_Project_Plan.html) (architecture diagram + 9-day schedule), [proposal-praxis.md](../plans/proposal-praxis.md), [AUDIT.md](../../AUDIT.md)  
**Live demo:** **Monday, June 29, 2026** — 10-minute Gauntlet capstone presentation  
**Demo calendar:** **Internal** freeze/practice **Thu–Fri Jun 26–27** (Sprint Days 9–10) · **Public** Gauntlet showcase **Mon Jun 29** — see [Scrum Master freeze gates](#demo-freeze--three-practice-runs-before-jun-29) below  
**Sprint snapshot:** Monica pillar mock-complete through Day 8 (React UI + Python contract layer); P0 eval cases `quirky_*` + poison good/bad landed with tests; Matthew live API + **PostgreSQL** still critical path for Acts 2–3 on staging

---

## Monica’s dual role this sprint

| Role | Scope | Success looks like |
|------|-------|-------------------|
| **Dashboard & Human Gate Lead** | `frontend/`, candidate API client, mock fixtures, demo Act 2 | Reviewers can promote with provenance visible; mock path demo-ready today |
| **Daily Scrum Master** | 15-min team sync, blocker triage, freeze/practice gates, checklist hygiene | Matthew and Dominic never surprised by integration dates; P0 visible every morning |

**Interview claim (dashboard):** *"I designed and built the human approval dashboard that enforces quality gates and makes knowledge promotion transparent and measurable."*

**Interview claim (scrum):** *"I kept a three-pillar capstone on track to a measured demo — daily alignment to the architecture plan, explicit freeze gates, and eval cases that proved the loop."*

---

## Daily Scrum Master — responsibilities

### Ritual

| Item | Detail |
|------|--------|
| **When** | 10:00 AM daily · 15 minutes max |
| **Template** | [STANDUP_TEMPLATE.md](STANDUP_TEMPLATE.md) |
| **Attendees** | Monica Peters (Tom Tarpey as advisor when available) |
| **Artifact** | Post standup notes; tick items in **this checklist** same day |

### Standing agenda (Monica runs)

1. **Yesterday done** — one line per pillar; link MR/commit when possible  
2. **Today committed** — max three items per person; must map to a Day N row below  
3. **Blockers** — owner, needed-by date, which P0 it threatens  
4. **Integration clock** — days until integration complete (Jun 24), feature freeze (Jun 25), hard freeze (Jun 27), demo (Jun 29)  
5. **Practice runs** — confirm next timed 10-min rehearsal date  
6. **Eval cases** — new YAML from Monica: Matthew acknowledged? Dominic wired into harness?

### Scrum Master escalation rules

| Situation | Monica action |
|-----------|---------------|
| Matthew API not started by **Day 6 AM (Jun 23)** | Flag P0; team agrees mock+fixture demo path for Jun 29 |
| Dominic metrics endpoint missing by **Day 8 (Jun 25)** | Act 3 uses [eval-metrics.json](../integration/fixtures/eval-metrics.json) with dated narration |
| Any pillar >1 day behind Day N milestone | Propose scope cut in standup; document in MR description |
| Post–hard-freeze (Jun 27) change request | Dominic + affected pillar lead approve; demo branch only |
| New eval case merged | Verify `pytest knowledge/evals/tests/ -q` green; notify Matthew for pipeline fixture use |

### Freeze & practice gates (Scrum Master owns calendar)

| Gate | Date | Rule |
|------|------|------|
| **Integration complete** | **Tue Jun 24 EOD** (Day 7) | Dashboard → real API; promote → graph write; eval cold + injected |
| **Feature freeze** | **Wed Jun 25 EOD** (Day 8) | Bugfixes + measurement only |
| **Hard freeze** | **Fri Jun 27 EOD** (Day 10) | Demo script locked; P0 blockers only |
| **Demo branch** | **Sun Jun 28 AM** | Tag `demo-jun29`; all rehearsals from that commit |

| Practice | Date | Pass criteria |
|----------|------|---------------|
| **Practice 1** | Wed Jun 25 | All 3 acts ≤10 min; mock dashboard OK |
| **Practice 2** | Fri Jun 27 | Live API + eval metrics preferred |
| **Practice 3** | Sun Jun 28 PM | Dress rehearsal; no code after unless P0 |

**Act budget:** Act 1 ~2.5 min · Act 2 ~3.5 min · Act 3 ~3.5 min · close ~0.5 min  
**Act 2 script:** [DEMO_SCRIPT.md](DEMO_SCRIPT.md) · **Full 3-act:** Dominic Day 9 deliverable

---

## Monica — eval cases for Matthew (architecture-aligned)

Monica authors **new eval cases** under `knowledge/evals/cases/<case_id>/case.yaml` for Matthew to use while building distillation, scoring, and API output. Cases must follow **Matthew’s and Dominic’s first eval structures** and align with boxes in [PRAXIS_Project_Plan.html](../plans/PRAXIS_Project_Plan.html) Figure 1.

### Matthew’s existing case patterns (copy these)

| Case | Path | Pattern | Architecture box |
|------|------|---------|------------------|
| `example_add_function` | `knowledge/evals/cases/example_add_function/case.yaml` | `seeded_insight.via_ingestor` + `direct_to_graph`; `deterministic_checks` + `rubric`; coding task | **Knowledge Graph** → **get context** → agent |
| `iambic_poem` | `knowledge/evals/cases/iambic_poem/case.yaml` | `direct_to_graph` only; domain-specific deterministic check + rubric | **Injected knowledge** steers Claude Code output |

**Monica cases shipped:** `pathlib_preference`, `docstring_policy`, `poison_negative_control` under `knowledge/evals/cases/` — covered in [test_cases.py](../../knowledge/evals/tests/test_cases.py).

**Registry total:** **53+** YAML cases on disk after main merge (Matt/Dominic sweep + Monica namespace).

**Frozen schema:** `knowledge/evals/eval_def.py` — `EvalCase`, `SeededInsight`, `DeterministicCheckRef`, `Rubric`  
**Loader tests:** `knowledge/evals/tests/test_eval_def.py` (`test_example_case_on_disk_loads`)  
**Case registry test pattern:** `knowledge/evals/tests/test_cases.py` — one test per on-disk case loads and meets pillar-specific assertions (Monica extends this file as she adds cases)

### Architecture diagram → eval case mapping

Each new Monica case must cite which diagram node it exercises (Figure 1 in project plan):

| Diagram node | What the eval proves | Monica case ideas (backlog) |
|--------------|----------------------|----------------------------|
| **Claude Code → Session Logs (DynamoDB)** | Raw JSONL exists for distillation input | Fixture path in case comment pointing to sample `logs/*.jsonl` line |
| **GitHub / Learning Moment Detection** | Trigger fires on PR or user insight | Case whose `seed_prompt` replays a “quirk” first seen in a faux PR context |
| **Trigger → LLM Distillation** | Structured lesson extracted | `via_ingestor` seeds that mimic distilled `{when, lesson, scope}` text Matthew’s pipeline should emit |
| **Consolidate • Score + Human Approval Gate** | Scored candidate before promote | Pair of `direct_to_graph` insights with conflicting scope → feeds contradiction UI + Matthew dedup |
| **Knowledge Graph** | Promoted knowledge persists | `direct_to_graph` entries that match [mock_data.py](../../frontend/mock_data.py) lesson titles |
| **get context tool call** | Just-in-time retrieval helps agent | Cold run (empty graph) vs seeded run on **same** `seed_prompt` — Dominic pairs; Monica authors the prompt |
| **Knowledge Graph Dashboard** | Human promotes correct lesson | Mock candidate `id` referenced in case README for demo Act 2 handoff |
| **Eval Harness** | Cold vs injected metrics | Case designed to **fail** FakeRunner baseline, **pass** with `seeded_insight` (mirror `example_add_function`) |

### Monica eval authoring checklist (per new case)

- [ ] Create `knowledge/evals/cases/<case_id>/case.yaml` using fields in `eval_def.py` only  
- [ ] Include header comment: diagram node(s), demo act (1/2/3), linked mock candidate id if any  
- [ ] At least one `deterministic_checks` entry **or** non-empty `rubric`  
- [ ] `seeded_insight.via_ingestor` when testing Matthew’s **Ingestor** path; `direct_to_graph` when testing **injection** only  
- [ ] Add test in `knowledge/evals/tests/test_cases.py`: `load_case` succeeds + case-specific assertion  
- [ ] Run `uv run pytest knowledge/evals/tests/test_cases.py knowledge/evals/tests/test_eval_def.py -q`  
- [ ] Notify Matthew in standup: case id, expected promoted lesson text, provenance string shape (`logs/<file>.jsonl:<line>`)  
- [ ] Optional: add matching row to `frontend/mock_data.py` so dashboard Act 2 shows the same lesson

### Suggested Monica eval backlog (Days 3–8)

| Priority | Case id (proposed) | Diagram focus | Demo act |
|----------|-------------------|---------------|----------|
| P0 | `quirky_exhaustive_switch` | Learning moment + injection | 1 + 3 | ✅ landed + tests |
| P0 | `quirky_config_load_order` | Contradiction / rival lessons | 2 | ✅ landed + tests |
| P1 | `promote_then_rerun` | Human gate → KG → get context | 2 + 3 |
| P1 | `decayed_lesson_ignored` | Decay rules | stretch |
| P2 | `cross_session_rediscovery` | Learning moment detection | stretch |

Dominic owns harness pairing (cold vs injected) and metrics; Monica owns **case YAML + dashboard/mock alignment**.

---

## Executive summary (team)

| Pillar | Owner | Days 3–5 gap | Days 6–7 gap | Demo risk |
|--------|-------|--------------|--------------|-----------|
| **ML & Knowledge Pipeline** | Matthew | **Critical** — distillation, dedup, scoring, API not built | **Critical** — E2E wiring, KG, get-context | **Highest** — blocks live Acts 2–3 |
| **Dashboard & Human Gate** | Monica | **Low** — Day 4–5 UI done on mock (React) | **Medium** — live API + eval embed | **Low on mock** — rehearse mock path until API lands |
| **Arch, Eval & Integration** | Dominic | **High** — PR replay, token/time, quirky repo | **High** — cold vs injected, metrics endpoint | **High** — compounding proof is demo climax |

**Verdict:** Vision and contracts aligned; **Matthew’s candidate API + pipeline** and **Dominic’s measurement spine** are critical path. Monica keeps both visible daily and feeds Matthew **architecture-aligned eval cases**.

---

## Calendar — sprint days vs real dates

| Sprint day | Calendar (EOD target) | Phase focus |
|------------|----------------------|-------------|
| 1–2 | Jun 16–19 (skip Jun 18) | Foundation & design — ✅ complete |
| **3–5** | **Jun 19–23** | Parallel pipeline + dashboard + eval tooling — Monica UI ✅ |
| **6** | **Mon Jun 23** | Integration sprint |
| **7** | **Tue Jun 24** | Human gate + injection complete |
| **8** | **Wed Jun 25** | Measurement & refinement · **Practice 1** — **← current posture** |
| **9–10** | **Thu–Fri Jun 26–27** | Demo polish · **Practice 2** · hard freeze |
| **Buffer** | **Sat–Sun Jun 28** | **Practice 3** · demo branch |
| **Showcase** | **Mon Jun 29** | **Live 10-min demo** |

---

## Day 3 — Core pipeline build

### Matthew — Full distillation + structured candidate + provenance

| Item | Status | Evidence / gap |
|------|--------|----------------|
| JSONL ingest + episode segmentation | ❌ | No `knowledge/ingest/` or JSONL parser |
| LLM distillation → structured candidate | ❌ | `PromptIngestor` only — line-split text, no `{when, lesson, scope, evidence, form}` |
| Provenance on every candidate | ❌ | `Insight` has `raw_text` only — no `logs/*.jsonl:line` |
| Learning-moment detector (carry from Day 2) | ❌ | Not started |

**Files to create/extend:**

- [ ] `knowledge/ingestion/` (rename from `injestion/` when ready)
- [ ] `knowledge/distillation/` or pipeline module emitting contract v1 `Candidate` shapes
- [ ] Wire provenance into objects matching [candidate-api-v1.md](../integration/candidate-api-v1.md)

### Monica — Candidate detail + confidence UI

| Item | Status | Evidence |
|------|--------|----------|
| Candidate detail view | ✅ | `frontend-react/src/components/CandidateDetail.tsx` |
| Confidence breakdown UI | ✅ | `frontend-react/src/components/ConfidenceBreakdown.tsx` |
| Provenance display | ✅ | Detail panel + list columns |
| Audit trail display | ✅ | `CandidateDetail.tsx` |
| React dashboard | ✅ | `frontend-react/` — mock + contract v1 client |
| **Eval cases for Matthew** | ✅ | 5 Monica namespace cases + 2 quirky P0; `test_cases.py` green |

### Dominic — Python tooling + basic injection

| Item | Status | Evidence |
|------|--------|----------|
| Reader / distiller wrapper | ⚠️ Partial | `knowledge/wiring.py`, `WholeFileReader`, `PromptIngestor` |
| Basic injection into agent run | ⚠️ Partial | `knowledge/evals/claude_code.py` — `--append-system-prompt` from graph reader |
| Fixed quirky repo / eval cases | ✅ | All **63** registered YAML cases map to mock rows (`eval_*` auto-gen + hand-crafted `cand_*` demos) |
| GitHub hook design | ❌ | No hook code in repo |

---

## Day 4 — ML consolidation + human gate workflow

### File-by-file trace — Day 4 milestones

#### Matthew — Embeddings + HDBSCAN + contradiction detection

| Planned artifact | Repo file(s) | Status | Notes |
|------------------|--------------|--------|-------|
| Embedding model / vector store | — | ❌ Missing | |
| HDBSCAN cluster/dedup | — | ❌ Missing | |
| Contradiction detection logic | — | ❌ Missing | |
| Candidate records with `contradictions[]` | API contract only | ❌ | Mock: `cand_9` ↔ `cand_16` in `frontend/mock_data.py` |

**Existing foundation:**

```text
knowledge/injestion/parent_injestor.py
knowledge/injestion/injestor_variants/prompt_injestor.py
knowledge/knowledge_graph/parent_knowledge_graph.py
knowledge/knowledge_graph/knowledge_graph_variants/in_memory_graph.py
knowledge/tests/test_injestor.py
```

#### Monica — Human gate workflow (`proposed → suggested → active`)

| Planned artifact | Repo file(s) | Status | Notes |
|------------------|--------------|--------|-------|
| State machine | `frontend/models/candidate.py` | ✅ | `next_promotion_state()` |
| Promote UI + confirmation | `frontend-react/src/components/CandidateTable.tsx`, `CandidateCards.tsx` | ✅ | Low-confidence warning (<50%); confirm dialogs |
| Mock promote flow | `frontend/services/mock_provider.py` | ✅ | Audit trail appended |
| Live promote via API | `frontend/services/api_client.py`, `frontend-react/src/api/apiClient.ts` | ✅ Client | Server at `knowledge/serve`; smoke when API URL set |
| List filter by state | `frontend-react/src/components/` + filter bar | ✅ | |
| Contract tests | `frontend/tests/test_mock_gate_workflow.py` | ✅ | 4 tests |
| Eval: `quirky_config_load_order` | `cases/quirky_config_load_order/` | ✅ | Aligns with `cand_9` ↔ `cand_16` |

#### Dominic — Eval: scripted PR/ticket recreation

| Planned artifact | Repo file(s) | Status | Notes |
|------------------|--------------|--------|-------|
| Case registry | `knowledge/evals/eval_def.py`, `cases/*/case.yaml` | ✅ | |
| Deterministic checks | `deterministic_checks/builds.py`, `poetry.py` | ✅ | |
| FakeRunner | `knowledge/evals/run.py` | ✅ | |
| Real Claude Code runner | `knowledge/evals/claude_code.py` | ✅ | |
| PR/ticket recreation | — | ❌ | |
| GitHub hook / PR automation | — | ❌ | |

---

## Day 5 — Scoring, decay, contradiction UI

### File-by-file trace — Day 5 milestones

#### Matthew — Confidence scoring + decay

| Planned artifact | Repo file(s) | Status | Notes |
|------------------|--------------|--------|-------|
| Freq / recency / breadth scoring | — | ❌ | |
| Decay rules (`active → decayed`) | — | ❌ | Mock `decayed` in `mock_data.py` only |
| `confidenceBreakdown` in API | Contract + mock | ⚠️ | `mock_data.py`, `ConfidenceBreakdown.tsx` |

#### Monica — Contradiction resolution + credibility viz

| Planned artifact | Repo file(s) | Status | Notes |
|------------------|--------------|--------|-------|
| Side-by-side contradiction panel | `frontend-react/src/components/ContradictionPanel.tsx` | ✅ | |
| Resolve via provider | `mock_provider.py`, `api_client.ts` | ✅ | Mock + Matthew server (`knowledge/serve`) |
| Credibility metrics viz | `ConfidenceBreakdown.tsx`, list columns | ✅ | |
| Reject with reason | `mock_provider.py`, detail UI | ✅ | |

#### Dominic — Token/time tracking + dashboard hooks

| Planned artifact | Repo file(s) | Status | Notes |
|------------------|--------------|--------|-------|
| Baseline writer | `knowledge/evals/run.py` → `results/baseline.jsonl` | ✅ | |
| Token/time in results | — | ❌ | |
| Eval metrics contract | `docs/integration/eval-metrics-v1.md` | ✅ | |
| Dashboard embed | `frontend-react/src/components/EvalMetricsEmbed.tsx` | ✅ | Placeholder curve |
| Metrics HTTP server | — | ❌ | Dominic P0 |

---

## Day 6 — Integration sprint (target Mon Jun 23)

#### Matthew — E2E pipeline + KG stub

| Planned artifact | Repo file(s) | Status | Notes |
|------------------|--------------|--------|-------|
| Pipeline orchestration | — | ❌ | `knowledge/run.py` smoke only |
| **PostgreSQL setup** | [RDS_KG_DEPLOY.md](RDS_KG_DEPLOY.md) | ❌ **P0** | Matthew — CDK RDS 16 + pgvector, bootstrap, `PRAXIS_DB_URL` or Secrets Manager; backs candidate list + promote mutations |
| Knowledge graph stub | `in_memory_graph.py` | ⚠️ | In-memory string → **PostgreSQL persistence target** |
| **Candidate REST API** | [`knowledge/serve/app.py`](../../knowledge/serve/app.py) | ✅ **P0 shipped** | FastAPI contract v1; JSON store + Postgres via `PRAXIS_DB_URL` |
| Server endpoints | `knowledge/serve/tests/test_server.py` | ✅ | Offline TestClient; live smoke: `frontend/tests/test_live_api_smoke.py` |
| Fixtures | `docs/integration/fixtures/*.json` | ✅ | 7 client tests |

**Minimum Day 6 server (Matthew):**

- [ ] PostgreSQL provisioned (CDK RDS or local Docker); connection via `PRAXIS_DB_URL` or AWS Secrets Manager — [RDS_KG_DEPLOY.md](RDS_KG_DEPLOY.md)
- [ ] FastAPI (or equivalent) serving contract v1
- [ ] Promote/reject/resolve persist to PostgreSQL; API reads reflect updated state
- [ ] Promote mutates store; returns updated `Candidate`
- [ ] `X-Praxis-Contract: 1` enforced
- [ ] Smoke: `cd frontend-react && npm run dev` with `VITE_PRAXIS_API_BASE_URL=http://localhost:8000`

#### Monica — Dashboard ↔ backend

| Planned artifact | Repo file(s) | Status | Notes |
|------------------|--------------|--------|-------|
| Provider factory | `frontend/services/data_provider.py` | ✅ | |
| API client | `frontend/services/api_client.py` | ✅ | |
| Wire-up doc | `docs/integration/wire-up.md` | ✅ | |
| Live integration smoke | — | ⚠️ | [`INTEGRATION_SMOKE.md`](INTEGRATION_SMOKE.md) + `test_live_api_smoke.py` when API up |
| Render deploy | `frontend-react/render.yaml` | ✅ | Static site + API blueprint |
| **Monica: integration smoke doc** | [INTEGRATION_SMOKE.md](INTEGRATION_SMOKE.md) | ✅ | Screenshots when API live |

#### Dominic — Cold vs injected runner

| Planned artifact | Repo file(s) | Status | Notes |
|------------------|--------------|--------|-------|
| Case loader + grader | `knowledge/evals/run.py` | ✅ | |
| Seeded insight path | `eval_def.py` → `SeededInsight` | ✅ | |
| Paired cold vs injected command | — | ❌ | |
| Correction counting | — | ❌ | |
| Compounding curve output | — | ❌ | |

---

## Day 7 — Human gate + injection complete (target Tue Jun 24)

#### Matthew — KG + get-context tool

| Planned artifact | Repo file(s) | Status | Notes |
|------------------|--------------|--------|-------|
| Get-context tool | `whole_file_reader.py` | ⚠️ | Dumps whole graph |
| Promote → graph write | — | ❌ | |
| Session history in context | — | ❌ | |

#### Monica — Full approval flow

| Planned artifact | Repo file(s) | Status | Notes |
|------------------|--------------|--------|-------|
| Full promote chain on mock | `test_mock_gate_workflow.py` | ✅ | |
| Provenance on every row | list + detail | ✅ | |
| Full flow on live API | — | ⚠️ | Run `test_live_api_smoke.py` + INTEGRATION_SMOKE §3 |

#### Dominic — Demo data prep

| Planned artifact | Repo file(s) | Status | Notes |
|------------------|--------------|--------|-------|
| Session capture → DynamoDB | `session-capture/.../dynamo.go` | ✅ | |
| Eval metrics endpoint | — | ❌ **P0** | [eval-metrics.json](../integration/fixtures/eval-metrics.json) shape |
| Demo data bundle | `mock_data.py`, fixtures | ⚠️ | Monica aligns eval cases + mock candidates |

---

## Day 8 — Measurement & refinement (target Wed Jun 25)

| Owner | Milestone | Status | Gap |
|-------|-----------|--------|-----|
| Matthew | Batch evals; tune thresholds | ❌ | |
| Monica | Dashboard polish + a11y | ⚠️ | Code labels shipped; manual SR pass in [DAYS_9_10_REMAINING.md](DAYS_9_10_REMAINING.md) |
| Monica | **Practice 1** facilitation | — | Wed Jun 25 |
| Dominic | Compounding curve measurement | ❌ | Demo climax |
| Team | Day 8 done criteria below | — | |

**Day 8 done for Jun 29:**

- [ ] ≥50% correction reduction on ≥1 benchmark case (or honest dated narrative)
- [ ] `PRAXIS_EVAL_METRICS_URL` serves real or batch data
- [ ] Dashboard live curve + scoreboard
- [ ] End-to-end: log → candidates → promote → eval improved
- [ ] Monica eval cases load in `test_cases.py`

---

## Day 9 — Demo polish (target Thu Jun 26)

| Owner | Milestone | Status |
|-------|-----------|--------|
| Monica | Demo-ready dashboard; user-flow video | ⚠️ Mock-ready |
| Dominic | Full 3-act demo script | ⚠️ Partial |
| Team | **Practice 2** Fri Jun 27 | — |

---

## Day 10 — Final runs (target Fri Jun 27)

| Owner | Milestone |
|-------|-----------|
| All | **Practice 3** Sun Jun 28 PM |
| All | Demo branch `demo-jun29` tagged |
| All | Backup recording |

---

## Cross-pillar P0 checklist (before feature freeze Jun 25)

### P0 — Blocks live demo

- [ ] **Matthew:** Candidate REST API per [candidate-api-v1.md](../integration/candidate-api-v1.md) — **shipped** (`knowledge/serve`); promote → graph write still open
- [ ] **Matthew:** Promote → `KnowledgeGraph.write`
- [ ] **Matthew:** Minimal JSONL → `Candidate` with provenance
- [ ] **Dominic:** Cold vs injected paired run + correction counts
- [ ] **Dominic:** Eval metrics GET per [eval-metrics-v1.md](../integration/eval-metrics-v1.md)
- [ ] **Dominic:** Quirky benchmark case for Acts 1 & 3 (Monica YAML + Dominic harness)
- [x] **Monica:** Live integration smoke doc once API exists — [INTEGRATION_SMOKE.md](INTEGRATION_SMOKE.md) (screenshots pending Matthew server)
- [x] **Monica:** ≥2 architecture-aligned eval cases merged + `test_cases.py` green — **5 Monica + 2 quirky P0** shipped
- [ ] **Team:** 3-act demo script with fallback matrix

### P1 — Credibility boost

- [ ] HDBSCAN dedup (Matthew)
- [ ] Contradiction detection in pipeline (Matthew)
- [ ] Confidence scorer (Matthew)
- [ ] Token/time in eval results (Dominic)
- [ ] GitLab CI pytest (team)
- [ ] Root `pyproject.toml` pythonpath fix (Monica)

---

## Integration contract readiness

| Contract | Client | Server | Tests |
|----------|--------|--------|-------|
| Candidate API v1 | ✅ `api_client.py`, `frontend-react/src/api/apiClient.ts` | ✅ `knowledge/serve/app.py` | ✅ offline + optional live smoke |
| Eval metrics v1 | ✅ `EvalMetricsEmbed.tsx` | ⚠️ `/metrics` fixture on Matthew API | ✅ fixture |
| Wire-up | ✅ `wire-up.md` | — | — |

---

## Test posture (baseline Jun 18)

| Suite | Command | Result |
|-------|---------|--------|
| Knowledge | `uv run pytest knowledge/ -q` | **39 passed** |
| Frontend | `$env:PYTHONPATH="frontend"; uv run pytest frontend/tests/ -q` | **14 passed** |
| Eval cases | `uv run pytest knowledge/evals/tests/test_cases.py -q` | **21 passed** |
| E2E loop | — | **None** |

---

## Demo day fallback matrix (Jun 29)

| Act | Full stack | Fallback |
|-----|------------|----------|
| 1 | Live Claude Code on quirky repo | Pre-recorded clip + JSONL path |
| 2 | Live API + React dashboard | Mock React — **demo-ready today** |
| 3 | Live re-run + metrics | Fixture metrics + “batch run Jun 26” narration |

---

## Related docs

| Doc | Use |
|-----|-----|
| [Monica-Peters-Dashboard-Plan.md](Monica-Peters-Dashboard-Plan.md) | Personal pillar schedule |
| [STANDUP_TEMPLATE.md](STANDUP_TEMPLATE.md) | Daily scrum notes |
| [DEMO_SCRIPT.md](DEMO_SCRIPT.md) | Act 2 beats |
| [DAYS_9_10_REMAINING.md](DAYS_9_10_REMAINING.md) | Manual polish |
| [ARCHITECTURE_MONICA.md](ARCHITECTURE_MONICA.md) | Dashboard architecture |
| [PRAXIS_Project_Plan.html](../plans/PRAXIS_Project_Plan.html) | Diagram + 9-day plan |
| [AUDIT.md](../../AUDIT.md) | Repo health snapshot |
| [wire-up.md](../integration/wire-up.md) | API wiring |
| [INTEGRATION_SMOKE.md](INTEGRATION_SMOKE.md) | Mock + live smoke tables |

---

*Monica updates this checklist at each daily sync. Tick ✅ when merged and verified by the owning pillar lead.*
