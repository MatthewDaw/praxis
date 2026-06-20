# Act 2 rehearsal log — 2026-06-19

Automated and agent-verified steps for [DEMO_SCRIPT.md](DEMO_SCRIPT.md). Timed live rehearsal and screen recording remain manual (see [DAYS_9_10_REMAINING.md](DAYS_9_10_REMAINING.md)).

## Automated gate (passed)

```text
uv run pytest knowledge/evals/tests/test_cases.py frontend/tests/ -q  → 35 passed
cd frontend-react && npm run lint && npm run build                    → OK
```

## Mock workflow script (passed)

Programmatic Act 2 beats against `MockDataProvider`:

| Beat | Action | Result |
|------|--------|--------|
| List | `list_candidates()` | ≥17 rows with provenance |
| Promote | `promote("cand_1")` | proposed → suggested |
| Contradiction | Pair `cand_9` ↔ `cand_16` exists | ✅ |
| Resolve | `resolve_contradiction(..., keep_id="cand_9")` | Rival `cand_16` removed |
| Chain | `promote("cand_2")` from suggested | → active |

## Manual rehearsal (Monica — before Practice 1)

- [ ] Timed React run: `cd frontend-react && npm run dev` (target ≤3.5 min)
- [ ] User-flow video capture (<3 min)
- [ ] Accessibility tab + screen reader pass per DAYS_9_10 checklist

## Live API rehearsal (when Matthew server up)

Follow [INTEGRATION_SMOKE.md](INTEGRATION_SMOKE.md) §3 and capture screenshots.
