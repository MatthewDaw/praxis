# Eval Metrics API — Contract v1

**Owner (client):** Monica Peters — `frontend/components/eval_metrics_embed.py`  
**Owner (server):** Dominic Antonelli — Eval harness  
**Version:** `X-Praxis-Contract: 1`

Async contract for the compounding-curve embed. Set `PRAXIS_EVAL_METRICS_URL` to a JSON GET endpoint. No pairing session required.

**Fixture:** [`fixtures/eval-metrics.json`](fixtures/eval-metrics.json)

---

## `GET` (eval metrics URL)

No request body. Optional header: `X-Praxis-Contract: 1`.

### Response fields

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `correction_rate` | number[] | yes* | Compounding curve Y-axis (0–1 or raw counts) |
| `sessions` | string[] | no | X-axis labels; length should match `correction_rate` when provided |
| `corrections_before` | number | no | Cold-run correction count (Act 3 scoreboard) |
| `corrections_after` | number | no | Post-injection correction count |

Aliases accepted by the dashboard: `correctionRate`, `correctionsBefore`, `correctionsAfter`.

\*When URL is unset, the dashboard shows a placeholder curve.

### Example

```json
{
  "correction_rate": [1.0, 0.72, 0.48, 0.35],
  "sessions": ["cold", "run_1", "run_2", "run_3"],
  "corrections_before": 12,
  "corrections_after": 5
}
```

---

## Dashboard behavior

- Line chart from `correction_rate` (and `sessions` index when lengths match).
- When `corrections_before` and `corrections_after` are present, shows cold vs PRAXIS metrics and % reduction.
- On fetch failure: warning banner + placeholder curve (mock demo still works).

---

## Self-serve validation

```powershell
cd frontend
$env:PYTHONPATH = "."
..\frontend\venv\Scripts\pytest tests/test_contract_fixtures.py::test_eval_metrics_fixture -q
```
