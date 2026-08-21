"""Praxis's generic model registry and campaign adjudication services.

The registry tracks an autoresearch campaign as three Praxis fact categories --
``model`` (a registered comparison contract), ``idea`` (a hypothesis to trial against
it) and ``trial`` (one run of an idea) -- each carrying required ``meta`` keys
(:mod:`knowledge.ml_registry.schema`). Once a model is registered, its judging
contract must not drift under a worker's feet mid-campaign
(:mod:`knowledge.ml_registry.guards`).

This is pure, DB-free validation logic. The actual write path that persists these
facts, the origin/ledger/budget business rules, and the adopt/park/reject lifecycle
are later tickets (R2+) built on this schema.
"""

from .contracts import RunsExport
from .storage import Registry
from .storage.importers import HistoricalLedgerImporter

__all__ = ["HistoricalLedgerImporter", "Registry", "RunsExport"]
