# Matthew — eval case handoff (2026-06-18)

Architecture-aligned code-target cases for distillation, provenance, and
injection validation. Run loader tests:

```powershell
uv run pytest knowledge/evals/tests/test_cases.py knowledge/evals/tests/test_eval_def.py -q
```

Cold vs injected pairing (Dominic): run the same case with empty vs full
`seeded_insight` via harness arms.

## Case registry

| Case id | via_ingestor | direct_to_graph | Mock candidate | Provenance target |
|---------|--------------|-----------------|----------------|-------------------|
| `quirky_exhaustive_switch` | Raw TS correction | Promoted lesson | `cand_1` | `logs/session_20260615.jsonl:88` |
| `pathlib_preference` | os.path → pathlib correction | — | (optional `cand_18`) | `logs/session_20260616.jsonl:201` |
| `docstring_policy` | Docstring + test policy | — | — | `logs/session_20260614.jsonl:102` |
| `quirky_config_load_order` | nushell session line | Post-resolution `cand_9` | `cand_9` / rival `cand_16` | `logs/nushell_contrib_20260611.jsonl:56` |
| `poison_negative_control` | via_ingestor + direct_to_graph | pathlib lesson only | — | — |
| `poison_negative_control_good` | — | Correct policy only | — | `cases/monica/poison_negative_control_good/` |
| `poison_negative_control_bad` | — | Policy + poison line | — | `cases/monica/poison_negative_control_bad/` |

## Expected Candidate shapes

### cand_1 — quirky_exhaustive_switch

- **title:** TypeScript Exhaustive Switch Pattern
- **content:** When using a switch statement on a discriminated union or enum, include a default case that assigns the value to a variable of type `never`. …
- **provenance:** `logs/session_20260615.jsonl:88`

### cand_9 — quirky_config_load_order (winning lesson)

- **title:** experimental_options Before Config Load
- **content:** Early-boot experimental flags in nushell are read from the `experimental_options` environment variable before `config.nu` is evaluated. …
- **provenance:** `logs/nushell_contrib_20260611.jsonl:56`
- **contradictions:** `["cand_16"]`

Rival `cand_16` details: [`quirky_config_load_order/README.md`](quirky_config_load_order/README.md).

### pathlib_preference (new candidate suggestion)

- **title:** Prefer pathlib Over os.path
- **content:** Stop using os.path for new code — use pathlib.Path; it's the project standard.
- **provenance:** `logs/session_20260616.jsonl:201`

## Demo act mapping

| Act | Cases |
|-----|-------|
| 1 — Dumb agent | `quirky_exhaustive_switch`, `pathlib_preference` (cold run) |
| 2 — Human gate | `quirky_config_load_order` (contradiction pair in dashboard) |
| 3 — Smart agent | All P0 cases (injected run) |
| Poison narrative | `poison_negative_control_good` vs `_bad` |
