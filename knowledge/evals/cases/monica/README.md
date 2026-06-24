# Monica Dashboard Human-Gate Eval Suite

This suite supports Monica Peters' Dashboard & Human Gate pillar and the live demo flow. It is grounded in:

- `docs/monica/DEMO_SCRIPT.md`
- `docs/monica/INTEGRATION_SMOKE.md`
- `docs/monica/Monica-Peters-Dashboard-Plan.md`
- `docs/plans/PRAXIS_Project_Plan.html`
- `docs/plans/proposal-praxis.md`
- `docs/integration/candidate-api-v1.md`

The cases live under the canonical eval registry path, `knowledge/evals/cases/monica/`, so the shared harness can discover them with `load_cases()` and they can be run as a focused Monica suite.

## Cases

| Case | Demo support | What it proves |
|------|--------------|----------------|
| `monica_demo_candidate_contract` | Beat 1 and Beat 2 | Candidate rows carry title, content, lifecycle state, confidence, provenance, confidence breakdown, and audit trail. |
| `monica_human_gate_staged_not_injected` | Beat 4 | Passively distilled proposed knowledge is not injected; only active human-approved knowledge is readable. |
| `monica_promotion_context_cand1` | Beat 4 and Act 3 handoff | A promoted `cand_1` lesson becomes graph context with provenance and the TypeScript `never` rule. |
| `monica_contradiction_pair_metadata` | Beat 3 | `cand_9` and `cand_16` are linked as a reviewable contradiction pair. |
| `monica_api_mutation_audit_trail` | Beat 3 and Beat 4 | Promote/resolve actions leave a human audit record and a visible lifecycle transition. |
| `monica_low_confidence_confirmation` | Beat 4 | Low-confidence candidates require explicit confirmation before promotion. |
| `monica_data_source_fallback_readiness` | Setup and live smoke | The dashboard can explain mock/live mode, contract version, API URL, and eval metrics source without coupling to Matthew's database. |
| `monica_eval_metrics_narrative` | Beat 5 | Metrics can support the demo claim: promoted knowledge reduces corrections by at least 50% without success-rate regression. |

## Verify

From the repository root:

```powershell
uv run pytest knowledge/evals/tests/test_monica_suite.py -q
```

For the broader merge gate that includes Monica's contract and UI smoke path:

```powershell
uv run pytest knowledge/evals/tests/test_cases.py frontend/tests/ -q

Push-Location frontend-react
npm test
npm run lint
npm run build
Pop-Location
```

The `npm` commands must run from `frontend-react/`; the repository root does not have a `package.json`.
