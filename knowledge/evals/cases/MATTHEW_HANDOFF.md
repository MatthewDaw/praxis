# Matthew — eval case handoff (2026-06-18)

Architecture-aligned code-target cases for distillation, provenance, and
injection validation. Run loader tests:

```powershell
uv run pytest knowledge/evals/tests/test_cases.py knowledge/evals/tests/test_eval_def.py -q
```

Cold vs injected pairing (Dominic): run the same case with empty vs full
`seeded_insight` via harness arms.

**Mock dashboard:** Every registered case under `cases/<case-id>/` (including
`quirky_*`) appears in mock data. P0 demo rows use `cand_*` ids; all
other cases auto-generate as `eval_<case_id>` via `frontend/eval_mock_bridge.py`
(exported to `frontend-react/public/mock-candidates.json` on Render build).

## Case registry

| Case id | via_ingestor | direct_to_graph | Mock candidate | Provenance target |
|---------|--------------|-----------------|----------------|-------------------|
| `quirky_exhaustive_switch` | Raw TS correction | Promoted lesson | `cand_1` | `logs/session_20260615.jsonl:88` |
| `pathlib_preference` | os.path → pathlib correction | — | `cand_18` | `logs/session_20260616.jsonl:201` |
| `docstring_policy` | Docstring + test policy | — | `eval_docstring_policy` | `logs/evals/monica/docstring_policy.jsonl:1` |
| `quirky_config_load_order` | nushell session line | Post-resolution `cand_9` | `cand_9` / rival `cand_16` | `logs/nushell_contrib_20260611.jsonl:56` |
| `poison_negative_control` | via_ingestor + direct_to_graph | pathlib lesson only | `eval_poison_negative_control` | `logs/evals/monica/poison_negative_control.jsonl:1` |
| `poison_negative_control_good` | — | Correct policy only | `cand_19` | `logs/session_poison_demo.jsonl:14` |
| `poison_negative_control_bad` | — | Policy + poison line | `cand_20` (rival `cand_19`) | `logs/session_poison_demo.jsonl:22` |
| `promote_then_rerun` | — | Post-promote active fact | `cand_21` | `logs/nushell_contrib_20260611.jsonl:56` |
| `decayed_lesson_ignored` | — | Active pathlib lesson only | `eval_decayed_lesson_ignored` | `logs/evals/monica/decayed_lesson_ignored.jsonl:1` |
| `decayed_lesson_ignored_reader` | — | Active marker retrieval (component) | `eval_decayed_lesson_ignored` | `logs/evals/monica/decayed_lesson_ignored.jsonl:1` |
| `cross_session_rediscovery` | Rediscovered exhaustive-switch lesson | — | `eval_cross_session_rediscovery` | `fixture/session_rediscovery.jsonl:2` |

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
| Promote → rerun | `promote_then_rerun` (post-promote `direct_to_graph` only) |
| Decay filter | `decayed_lesson_ignored` + `decayed_lesson_ignored_reader` |
| Cross-session | `cross_session_rediscovery` |
| Poison narrative | `poison_negative_control_good` vs `_bad` |
