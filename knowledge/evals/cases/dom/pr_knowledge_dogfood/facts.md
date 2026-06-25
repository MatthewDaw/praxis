# Curated fact set — provenance

The hand-curated, high-signal facts pushed into the **treatment** arm of every task (via each
treatment case's `seeded_insight.direct_to_graph`, dumped unranked by the whole-file reader). Each
traces to a real merged Praxis PR or commit. Facts #1–#3 are the footgun-neutralizing facts the
paired tasks turn on; the rest are durable decisions/gotchas/conventions that round the set out to a
realistic small knowledge base (~13 facts, within the plan's 10–20 band).

The canonical text lives here; each treatment `case.yaml` embeds the **same** list verbatim. If you
edit a fact, update it in this file and in all three treatment cases.

| # | Source | Category | Fact |
|---|--------|----------|------|
| 1 | `d892e88` (#57) | gotcha | UMAP `n_neighbors` must stay low (`min(10, n-1)`, not 15) in `clustering.py::_reduce`. |
| 2 | `22db05f` | gotcha | `setup_tracing()` must run at module-import scope, not in `__main__` (uvicorn string-imports). |
| 3 | `f3eecfe` | convention | Replacement is a directional `supersedes` edge; custom resolution = supersession, not contradiction. |
| 4 | `f3eecfe` | decision | Contradiction is per-(subject,attribute)-slot, not transitive across slots. |
| 5 | `083d187` | invariant | FR-005: never two mutually-contradicting facts both `active`. |
| 6 | `dcd99d4` (#56) | gotcha | Secrets-Manager DSN fallback is gated behind `PRAXIS_DB_ALLOW_REMOTE=1`; local scripts must load `.env`. |
| 7 | `361bd9e` (#58) | convention | Graph is partitioned by `(org_id, user_id)`; snapshots scoped to the caller; fold-in writes only the caller's graph. |
| 8 | `361bd9e` (#58) | convention | MCP tools are thin authenticated wrappers over backend routes; authz inherited from the backend. |
| 9 | `1fdb8be` (#48) | gotcha | yoyo migrations import `knowledge` lazily (inside the step), so they load outside yoyo's loader. |
| 10 | `dcd99d4` (#56) | architecture | Slot-granular dedup/conflict resolution lives in the ingest path; `graph.write` is a direct write with no dedup. |
| 11 | harness (`run.py`) | convention | `direct_to_graph` seeds `active`; `via_ingestor` seeds `proposed` unless `ingest_state: active`. |
| 12 | `5e95bba` | gotcha | Claim/verdict cassettes are keyed on the rendered prompt; change a prompt → re-record or replay misses. |
| 13 | `d892e88` (#57) | testing | Clustering regression guards run production `_reduce` + HDBSCAN over a frozen embedding matrix, offline. |

## Canonical fact text (verbatim, as seeded)

1. In `knowledge/knowledge_graph/clustering.py` `_reduce`, keep UMAP `n_neighbors` low — use `min(10, n - 1)`, not 15. A high `n_neighbors` over-weights global structure and collapses a heterogeneous corpus into one mega-cluster (a 114-fact corpus melted into two blobs at 15; 10 recovers ~14 coherent topics).
2. Call `setup_tracing()` at module-import scope in the serve entrypoint (`knowledge/serve/app.py`), never inside `if __name__ == "__main__"`. uvicorn imports the app by string and never executes the `__main__` block, so tracing set up there never runs. It is a no-op unless `PHOENIX_COLLECTOR_ENDPOINT` is set.
3. When a new fact replaces old ones, link them with a directional `supersedes` edge — not a `contradicted_by` edge. Custom resolution is modeled as supersession (reject every member of the slot-cluster, write the new fact as a normal add), not as a fabricated contradiction.
4. Contradiction is per-slot, not transitive: pending contradictions cluster by the `(subject, attribute)` slot each edge was detected on, not by connected component. A chained A–B–C conflict splits into two slot-clusters and must never fabricate an A–C link.
5. Write policy enforces FR-005: the graph must never hold two mutually-contradicting facts both in the `active` state.
6. The DB DSN resolver (`knowledge/serve/db.py`) only falls back to the AWS Secrets Manager (production RDS) secret when `PRAXIS_DB_ALLOW_REMOTE=1` (set in App Runner + CI). Local scripts must load `.env` (`PRAXIS_DB_URL`) — otherwise they get no DB rather than silently connecting to prod.
7. Everything in the knowledge graph is partitioned by `(org_id, user_id)`. Snapshot save/load/delete are scoped to the caller's own partition; org snapshot browse/fold-in can read any org member's snapshots, but fold-in writes only into the caller's graph.
8. MCP tools are thin authenticated wrappers over existing backend routes — an agent can perform every graph/snapshot edit the dashboard UI can, with authorization inherited from the backend (no separate authz in the tool layer).
9. yoyo migrations must import the `knowledge` package lazily (inside the migration step, not at module load), so a migration module like `reject_rename` can be imported outside yoyo's loader.
10. Slot-granular dedup and conflict resolution live in the ingestion path (`ingest_dump` / `/ingest`), which distills each fact into a granular `(subject, attribute, value)` claim. `graph.write(...)` is a direct write with no dedup — don't expect it to reconcile slots.
11. Eval seeding: `seeded_insight.direct_to_graph` writes facts `active` (simulating user approval); `via_ingestor` writes them `proposed` (gated out of retrieval) unless the case sets `ingest_state: active`.
12. Claim/verdict cassettes are keyed on the rendered prompt text. If you change a prompt, re-record the cassette or offline replay misses loudly.
13. Clustering regression guards run the production `_reduce` + HDBSCAN over a frozen real-embedding matrix offline (no network, no labeling LLM) and assert segmentation (e.g. more than two clusters, no single cluster owning half the corpus).
