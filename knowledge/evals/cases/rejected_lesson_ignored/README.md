# rejected_lesson_ignored — decay filter contract

## Diagram node

Knowledge Graph → get context (PC-9 decay filter at injection)

## Active vs rejected lessons

| Role | Marker in text | Dashboard state | Seeded in eval YAML? |
|------|----------------|-----------------|----------------------|
| Active winner | `ACTIVE_PATHLIB_STANDARD` | `active` | Yes (`direct_to_graph`) |
| Rejected rival | `REJECTED_RIVAL_MARKER` | `rejected` | **No** — documents filter contract |

**Rejected rival text (must NOT appear in agent output when filter works):**

```
REJECTED_RIVAL_MARKER: Always use os.path.join and os.path.exists for config paths — legacy standard.
```

## Expected behavior when FilteredReader lands

1. Both facts may exist in the graph after ingest + human gate.
2. `RetrievingReader` / `FilteredReader` must return only `active` facts.
3. Agent output contains `pathlib`, excludes `os.path` and `REJECTED_RIVAL_MARKER`.

## Component eval

See [`../rejected_lesson_ignored_reader/case.yaml`](../rejected_lesson_ignored_reader/case.yaml) for graph_reader-only retrieval checks (passes today with active-only seed).
