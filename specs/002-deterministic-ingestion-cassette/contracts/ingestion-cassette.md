# Contract: `IngestionCassette`

A `str → str` keyed-replay store over the ingestion splitter, mirroring `CachedEmbedder` one layer
up. Internal eval-infra interface (no external/wire surface).

## Interface

```text
IngestionCassette(path, *, model_id: str, inner: Callable[[str], str] | None, allow_compute: bool)

__call__(raw_input: str) -> str        # the str→str shape PromptIngestor's LLM callable expects
```

## Behavior

| Precondition | Outcome | Requirement |
|--------------|---------|-------------|
| `key(raw_input)` in committed cassette | return the recorded `output_text`; **no live call** | FR-001, FR-002 |
| miss, `allow_compute` true and `inner` set | call `inner(raw_input)`, record under key, persist, return | FR-003 (record) |
| miss, recording disabled | raise `RuntimeError` naming the model + a refresh instruction | FR-003 (loud) |
| (caller) no committed cassette and no key | case is skipped, not run | FR-005 |

- **Key**: `sha256(f"{model_id}\n{raw_input}")` — includes the model id (FR-004).
- **Persistence**: JSON `{key: output_text}`, sorted keys, written under a process lock on save
  (mirrors the verdict/embedding caches so parallel `--workers` recording can't clobber).
- **No author-facing change**: callers still pass/receive a `str → str` callable (FR-006).

## Invariants

- Replaying a committed cassette issues **zero** live ingestion calls (SC-002).
- Re-running the same inputs yields identical output text every time (SC-001).
- A stale/missing key under replay-only is always a loud failure, never a silent stale pass (SC-003).
