.PHONY: check-ml-registry

## Canonical offline validation entry point. The environment assignment is part of the target so
## a developer or CI job cannot accidentally resolve a remote database during collection.
check-ml-registry:
	PRAXIS_DB_DISABLED=1 uv run pytest knowledge/ml_registry/tests -q -p no:cacheprovider
	uv run ruff check .

check-engine:         ## knowledge suite, DB seam disabled (no Docker needed)
	PRAXIS_DB_DISABLED=1 uv run pytest -q --ignore=knowledge/serve/tests

check-factory:        ## agent_factory suite (its conftest needs the repo root importable)
	PYTHONPATH=. uv run pytest agent_factory/tests -q

check-lint:           ## ruff over the engine and factory source
	uv run ruff check knowledge agent_factory

check-all: check-engine check-factory check-lint
