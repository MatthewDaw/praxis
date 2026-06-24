# skills_ingestion eval

Full-pipeline ingestion + retrieval case. Seeds the knowledge graph with **10
real, mutually-similar developer-workflow skills** pulled from
[skills.sh](https://skills.sh/) (the "code quality / dev workflow" cluster),
then asks the agent to recommend the right skill for a concrete task using
**only** the ingested knowledge.

The cluster is deliberately overlapping so the case tests more than "did the
text land":

- Two **TDD** skills (`obra/superpowers/test-driven-development`,
  `mattpocock/skills/tdd`) — near-duplicate intent, different emphasis.
- Two **debugging** skills (`obra/superpowers/systematic-debugging`,
  `mattpocock/skills/diagnose`) — same goal (root-cause first), different framing.
- Two **code-review** skills that are *complementary*, not redundant
  (`requesting-code-review` is the reviewer-dispatch side, `receiving-code-review`
  is the author side), plus a terse-format variant (`caveman-review`).
- Two **branch/worktree** git skills and one **architecture** skill.

What the case exercises:

1. **Ingestion** — 10 skill docs go through the ingestor (`ingest_state: active`)
   so they are retrievable.
2. **Retrieval** — the `retrieving` reader must surface the *task-relevant*
   skills for the seed prompt, not the whole pile.
3. **Grounding** — the answer must cite only skills that were actually ingested,
   and must notice the duplicate/overlapping pairs rather than inventing a single
   canonical skill.

Sources for each skill live under `sources/skills/` for provenance; the
canonical ingested text is the `via_ingestor` block in `case.yaml`.
