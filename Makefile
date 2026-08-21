.PHONY: check-ml-registry

## Canonical offline validation entry point. The environment assignment is part of the target so
## a developer or CI job cannot accidentally resolve a remote database during collection.
check-ml-registry:
	PRAXIS_DB_DISABLED=1 uv run pytest knowledge/ml_registry/tests -q -p no:cacheprovider
	uv run ruff check .
