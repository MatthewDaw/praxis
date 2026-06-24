"""Pytest session bootstrap (repo root).

Load the repo-root ``.env`` before any test imports, so the whole suite resolves
the same ``PRAXIS_DB_URL`` (and API keys) as the app and the migrations. Without
this, only modules that call ``load_dotenv()`` themselves (app, migrations,
``test_server``) saw ``.env``; the facts/graph tests fell through to AWS Secrets
Manager and ran against the deployed RDS. Loading here makes ``.env`` the single
source of truth for local test runs.

``load_dotenv`` does not override variables already set in the environment, so an
explicit shell ``export`` still wins (e.g. CI).
"""

from dotenv import load_dotenv

load_dotenv()
