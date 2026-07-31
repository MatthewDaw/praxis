---
title: The test suite refuses any non-local database — allowlist guard in the repo-root conftest
date: 2026-07-31
category: conventions
module: conftest.py (repo root), knowledge/serve/db.py (`require_local_dsn`, `is_local_dsn`, `bootstrap`)
problem_type: convention
component: testing
severity: critical
related_components: [db, migrations, yoyo, ci, justfile, docker-compose]
applies_when:
  - Running `pytest` anywhere in this repo
  - Wondering why pytest exited immediately with "REFUSING TO RUN"
  - Changing `PRAXIS_DB_URL`, the repo `.env`, or `db.resolve_dsn()`
  - Adding a fixture that calls `db.bootstrap()` or `db.connect()`
  - Debugging yoyo `LockTimeout` / `duplicate key ... yoyo_lock_pkey`
  - Tempted to put the production check inside `resolve_dsn()` for all callers
tags: [praxis, testing, production-safety, postgres, dsn, yoyo, migrations, pytest, conftest, docker]
---

# Tests must never run against the production database

## What went wrong

The repo `.env` sets `PRAXIS_DB_URL` to the deployed RDS instance
(`praxisknowledgegraphdbstack-…rds.amazonaws.com:5432/praxis_kg`) — **the same
instance and database the live backend at `https://uvrzcth5sx.us-east-1.awsapprunner.com`
serves**. The repo-root `conftest.py` loads `.env` for the whole suite, so a plain
`pytest -q` on a laptop wrote directly to production. Observed, not theoretical:

- Test orgs, facts and snapshots created in the production database.
- The suite took the **yoyo migration lock on production**; concurrent runs then
  failed with `LockTimeout` and `duplicate key value violates unique constraint
  "yoyo_lock_pkey"`, and a killed run left a stale lock row that had to be
  deleted by hand.
- The burst of test writes wedged the live backend's shared connection and
  cascaded **500s onto unrelated reads and writes** for a person using the live
  API at the time (the failure mode documented in the `_ConnProxy` docstring in
  `knowledge/serve/app.py`, gaps H13.2/H13.3). Hours were lost chasing a
  "regression" that was contention with a local test run.

CI never had this problem: the `python-test` job runs against an ephemeral
`pgvector/pgvector:pg16` service container on `localhost:5432`.

## The rule

**The pytest session refuses to start unless the resolved DSN is provably local.**

`conftest.py` (repo root) calls `db.require_local_dsn()` from `pytest_configure`,
before a single test is collected — a warning during a long run is invisible, so
this is a hard stop (`pytest.UsageError`, exit code 4).

Recognition is an **allowlist**, not a denylist: only `localhost`, `127.0.0.1`,
`0.0.0.0`, `::1`, `*.localhost` and the unix-socket (no host) form pass. A
denylist of known prod hostnames fails open — the day a second RDS instance is
created, or a bastion/tunnel hostname is used, it silently permits production
again. Unparseable DSNs fail closed. Both DSN spellings libpq accepts are
handled (URL and `host=… dbname=…` keyword form), and userinfo cannot spoof the
host (`postgresql://localhost:pw@evil.example.com/…` is refused).

The guard is on the **test path only**. It is deliberately *not* inside
`resolve_dsn()`/`connect()`: the MCP server, the Claude hooks and the
`agent_factory` tooling legitimately talk to the deployed database.

## How to run the tests

```sh
just db-up          # docker compose up -d --wait db  (pgvector/pgvector:pg16, host port 5433)
just db-bootstrap   # apply migrations/ to the local DB — first time, or after `just db-down`
just test           # pins PRAXIS_DB_URL at the local DB and runs pytest
just test -k facts  # extra args pass through
```

`just test` pins `PRAXIS_DB_URL=postgresql://praxis:praxis@localhost:5433/praxis_kg`
rather than trusting `.env`. Equivalently, export that URL in your shell, or
restore the local line in `.env` (it is there, commented out, directly above the
RDS one).

No Docker? `just test-nodb` runs the DB-free subset (798 tests):
`PRAXIS_DB_DISABLED=1 pytest --ignore=knowledge/serve/tests`. DB-backed modules
are `skipif`-gated on `db.resolve_dsn() is None`, but `knowledge/serve/tests` has
to be excluded rather than skipped: importing `knowledge.serve.app` builds
`app = create_app()` at module scope, which raises before any skipif is
consulted (34 collection errors). Fixing that would mean making the module-level
`app` lazy in `app.py` — worth doing, out of scope here.

## The escape hatch

```sh
PRAXIS_TESTS_ALLOW_REMOTE_DB_I_KNOW_THIS_MAY_WRITE_TO_PRODUCTION=1 pytest -q
```

Long and self-incriminating on purpose — nobody sets it by accident, and it reads
as a confession in shell history. When set, every run prints a three-line banner
to stderr naming the remote host (password redacted). Do not put it in `.env`,
a shell rc, or CI.

## Migration-lock hygiene (`db.bootstrap`)

Dozens of fixtures call `db.bootstrap()`, once per fixture. It is now:

- **memoized per DSN per process** — the schema cannot move under a running
  process, so repeat calls return immediately (`force=True` re-checks);
- **lock-free on the happy path** — the pending set is computed *before* taking
  the yoyo lock, and the lock is only taken when there is something to apply, so
  an up-to-date database never locks at all (this is what used to serialise every
  concurrent run behind one lock row);
- **patient when it does lock** — `PRAXIS_DB_LOCK_TIMEOUT` (default 60s) replaces
  yoyo's 10s default, with a re-check under the lock.

## Verification status

`knowledge/serve/tests/test_production_db_guard.py` covers the DSN cases (local,
CI, IPv6, keyword form, prod RDS, a *different* RDS host, spoofed userinfo,
garbage, `None`), the refusal text, password redaction, and the `pytest_configure`
hook itself — no database required. The refusal was also confirmed end to end: a
real `pytest` invocation with the stock `.env` exits 4 with the message and runs
nothing.

**Not verified:** the Docker path. Docker was unavailable on the machine where
this was written, so `just db-up` / `just db-bootstrap` were not executed. The
compose service is unchanged and pre-existing (`docker-compose.yml`, host port
5433, pgvector `initdb` hook); `just db-bootstrap` was changed only to pin the
local DSN so it can no longer migrate production.
