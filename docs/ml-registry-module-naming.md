# ml_registry module naming: plan vs. actual

`CODEX-ML-CAMPAIGN-FOUNDATION-AUDIT-PLAN.md` §5.5 prescribes a target Praxis tree under
`praxis/knowledge/ml_registry/` that names three modules which do not exist under those exact
names in the current tree. In each case the described behavior exists, just factored under a
different name. This note exists so a future reader searching for the plan's names does not
conclude the work is missing — search the "actual" column instead.

| §5.5 prescribes | Actual location | Why the name differs |
|---|---|---|
| `runtime/leases.py` | `contracts/lease.py` (`CampaignLease`, `LeaseSet` — the data model) + `runtime/ownership.py` (`LeaseIntentCoordinator` — acquire/release/heartbeat coordination) | The plan's §5.3 "Resource leases and isolation namespaces" is one paragraph, but the implementation split cleanly along the project's existing `contracts/` (pure data, no I/O) vs. `runtime/` (process/state ownership) boundary — the same split every other domain object in this tree follows. Merging them into one `runtime/leases.py` would put a Protocol-only dataclass in the same file as a coordinator that does atomic file I/O and process bookkeeping, which is exactly the pattern every other module in this tree avoids. |
| `runtime/reconcile.py` | `controller.py::ExecutorProcessBackend.reconcile` (restart reconciliation) plus `PortfolioController`'s own poll-time reconciliation path in the same file | §5.4's restart-reconciliation algorithm reads `process.json`/`state.json` and decides adopt/finalize/supersede — that decision needs the same backend state (`self._processes`, `self.root`) `ExecutorProcessBackend.submit`/`poll`/`cancel` already hold. A standalone `reconcile.py` would need to either duplicate that state or import the backend anyway, so it was kept as a method on the backend that already owns the process bookkeeping. |
| `services/finalize.py` | `services/registry_finalize.py` (`RegistryFinalizer`, `RegistryFinalizeService`) | Same responsibility (§5.4: "the only writer of the `production` alias and the only code that may mark a campaign complete") under a name that disambiguates it from `runtime/registry_completion.py` (a narrower predicate helper) once both existed side by side in the same package — `finalize.py` next to `registry_completion.py` read as two names for the same thing from an import line alone. |

None of these are a rename debt: the plan's names describe the *responsibility*, not a required
file path, and every responsibility named in §5.3/§5.4 is implemented and covered by
`knowledge/ml_registry/tests/test_controller.py` and
`knowledge/ml_registry/tests/test_runtime_process_ownership.py`. This note is a map, not a task —
do not do a blind rename sweep to match §5.5 literally; that is risk without benefit for code that
already has passing tests and real callers under its current name.
