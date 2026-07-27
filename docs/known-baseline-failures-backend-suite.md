---
title: Known pre-existing backend-suite failures (baseline, unrelated to any one ticket)
category: convention
module: knowledge/serve
component: test-suite
tags: [backend-suite, box-service, ci]
applies_when: the universal backend-suite check (knowledge/serve/tests) is run for a ticket in af-build-remote-jobs
---

# Known pre-existing `knowledge/serve/tests` failures

Verified on `af-build/praxis` at `e1a920e` (before any remote-jobs/box-service ticket work),
by stashing every remote-jobs change and re-running each failing test alone — every one of
these fails identically with no box-service code present. They are unrelated drift from other
already-merged tickets/salvaged WIP (`git log` shows `wip: salvage uncommitted worker output`
commits touching `knowledge/serve/app.py` around the same time), not a regression any single
ticket introduced.

The full, current, machine-readable set lives in `knowledge/serve/tests/known_baseline_failures.txt`
(one pytest node id per line; `scripts/run_backend_suite_scoped.sh` deselects exactly this list) —
re-verified 2026-07-27 while landing R72 (mailbox undelivered job view): a full-suite run showed
82 failures across `test_episodic_memory.py`, `test_mounts.py`, `test_org_sharing.py`,
`test_server.py`, `test_session_ingest.py`, `test_space_org_delete.py`, `test_spaces.py`,
`test_surface_bindings.py`, `test_compounding_read_surface.py`, and `test_batch_parallel.py`; every
one of them also fails run alone/isolated (not suite-order flakiness), with zero box-service or
mailbox code touched. Most surface as `400 Bad Request: "space required"`, a stale response shape,
or (the parallel-batch and `as_of` cases) unrelated pre-existing breakage in those surfaces.

Additionally, `test_server.py::test_edit_default_is_literal_write` is intra-suite-order flaky —
it fails intermittently in a full-suite run (state bleed through the shared `"default"` org from
tests earlier in the run) but passes every time run alone or with a fresh DB. It is NOT deselected
(it is not deterministically broken), just noted as a known source of full-suite flakiness
pre-dating this ticket.

The list is excluded (`--deselect`, via `scripts/run_backend_suite_scoped.sh`) from a ticket's own
backend-suite validation run so the ticket is graded on whether **it** regressed the suite, not on
unrelated pre-existing breakage — mirroring `agent-factory-suite`'s own documented
`-k "not test_factory_project_from_dotenv..."` exclusion for a different tracked pre-existing bug.
Remove entries from `known_baseline_failures.txt` once each underlying bug is actually fixed by
whichever ticket owns that surface; do not leave this permanently. R72's own new tests
(`knowledge/serve/tests/test_box_service_mailbox.py`) and touched files
(`knowledge/serve/box_service_mailbox.py`) carry none of this drift.
