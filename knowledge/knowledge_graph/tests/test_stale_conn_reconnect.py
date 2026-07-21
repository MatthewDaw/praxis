"""Locks reconnect-on-stale for the incomplete-requirements DB path (no Postgres needed).

A long-lived serve process kept ONE persistent connection per thread. When the DB dropped that
socket (restart / idle timeout), the first statement issued afterward raised before the app's
``_ConnProxy`` could reopen it — and that path was ``_server_epoch``, the first query in
``incomplete_requirements``, so ``GET /requirements/incomplete`` 500'd while endpoints that opened a
fresh connection first kept working. ``_execute_resilient`` retries once so the read self-heals.

We drive ``_server_epoch`` through a faithful re-implementation of the app's proxy (resolve fresh on
each access; reopen a broken/closed connection) over fake connections, so no real DB is involved.
"""

import psycopg
import pytest

from knowledge.knowledge_graph.knowledge_graph_variants.postgres_vector_graph import (
    PostgresVectorGraph,
)


class _FakeCursor:
    def __init__(self, val):
        self._val = val

    def fetchone(self):
        return (self._val,)


class _FakeConn:
    """A stand-in psycopg connection. ``die_once`` raises a stale-connection error on its first
    execute and marks itself broken (as psycopg does), so the proxy reopens on the next access."""

    def __init__(self, epoch, die_once=False):
        self.closed = False
        self.broken = False
        self._epoch = epoch
        self._die_once = die_once

    def execute(self, sql, params=None):
        if self._die_once:
            self.broken = True
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        return _FakeCursor(self._epoch)


class _ProxyLike:
    """Mirror of app._ConnProxy over a resolver: forward every attribute to the freshly resolved
    connection, so a retry after a break transparently uses a new connection."""

    def __init__(self, conns):
        self._conns = list(conns)
        self._idx = 0
        self._current = None

    def _resolve(self):
        c = self._current
        if c is not None and not c.closed and not getattr(c, "broken", False):
            return c
        c = self._conns[self._idx]
        self._idx += 1
        self._current = c
        return c

    def __getattr__(self, name):
        return getattr(self._resolve(), name)


def _graph(proxy):
    # embedder truthy (skip OpenRouterEmbedder), empty policy (skip default_write_policy) — we only
    # exercise the stale-retry, no embedding/writes.
    return PostgresVectorGraph(proxy, "org", "user", embedder=object(), policy=[])


def test_server_epoch_reconnects_after_stale_drop():
    dead = _FakeConn(0.0, die_once=True)   # raises + marks broken on first use
    live = _FakeConn(1234.5)               # the reopened connection
    g = _graph(_ProxyLike([dead, live]))
    assert g._server_epoch() == 1234.5     # retried on the fresh connection instead of 500ing


def test_healthy_connection_is_not_retried():
    live = _FakeConn(42.0)
    g = _graph(_ProxyLike([live]))
    assert g._server_epoch() == 42.0


def test_non_connection_error_propagates():
    class _BadConn(_FakeConn):
        def execute(self, sql, params=None):
            raise psycopg.errors.UndefinedColumn("boom")  # a real query error, NOT a stale conn

    g = _graph(_ProxyLike([_BadConn(0.0)]))
    with pytest.raises(psycopg.errors.UndefinedColumn):
        g._server_epoch()  # must NOT be swallowed/retried
