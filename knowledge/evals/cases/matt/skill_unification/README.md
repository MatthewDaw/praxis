# Skill-unification eval (Matt)

Goal: stress-test the **ingestion pipeline** on a large, partly-overlapping
corpus of agent "skills" drawn from two independent toolkits, then have the agent
synthesize a single **unified, de-duplicated** skill catalog out of the ingested
knowledge graph.

## Sources

Every `SKILL.md` from two repos was vendored once into `sources/`:

- **`sources/gstack/`** — Garry Tan's gstack (<https://github.com/garrytan/gstack>),
  58 skills (top-level `<name>/SKILL.md` plus the four `openclaw/` variants).
- **`sources/compound-engineering/`** — Every's compound-engineering plugin
  (<https://github.com/everyinc/compound-engineering-plugin>), 27 skills
  (`skills/<name>/SKILL.md`).

Filenames are the flattened skill paths (`/` → `__`; the gstack root `SKILL.md` is
`_root.md`). The full file body is kept — nothing is trimmed.

## Design

A **full-pipeline case** (`component: null`), modeled on `../applications/`:

1. **Raw ingestion.** All 85 skill files are fed in full through
   `seeded_insight.via_ingestor`, each prefixed with a `# source: <repo>/<skill>`
   provenance header. `ingest_model: openai/gpt-4o-mini` distils each one into the
   graph; the `vector` substrate + `cached` embedder let semantically-overlapping
   skills dedup at write time. No `direct_to_graph` — the point is to exercise
   distillation + dedup on messy real source text.
2. **Unification is the query.** The job posting is *not* the model here — the
   `seed_prompt` instructs the boxed agent to read ONLY the injected graph and
   write a unified, merged skill catalog to `unified-skills.md`. Bash/WebFetch/
   WebSearch are off, so it cannot re-fetch the repos.
3. **Grading.** `deterministic_checks` assert the catalog is non-empty, the file
   was written, and the capabilities that recur across both toolkits (planning,
   code review, design review, debugging, commit/PR, ship/release) all survive. A
   `rubric` grades coverage, dedup/merge quality, provenance, grounding, and
   structural coherence.

Chain under test: **85 skill files → ingest/distil/dedup → retrieve → agent
synthesizes a unified catalog → graded.**

## Regenerate

`sources/` is the source of truth; `case.yaml` is generated. To re-vendor or edit
the prompt/rubric, edit `_generate.py` and re-run:

```powershell
uv run python knowledge/evals/cases/matt/skill_unification/_generate.py
```

To re-vendor the skill files themselves, re-download every `SKILL.md` from both
repos into `sources/gstack/` and `sources/compound-engineering/` (flattening
paths as above), then re-run `_generate.py`.

## Run

```powershell
# real Claude Code (subscription): ingests 85 skills, writes unified-skills.md, grades
uv run python -m knowledge.evals.run matt_skill_unification
```

This is a full-pipeline `file_io` case (it needs a file-producing agent), so the
offline `--fake` backend skips it. Ingestion runs a real distillation model
(`OPENROUTER_API_KEY`) until the ingestion + embedding cassettes are recorded.
