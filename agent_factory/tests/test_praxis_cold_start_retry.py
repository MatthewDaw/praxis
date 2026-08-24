"""Praxis scales to zero; a cold start is not an outage.

The client is fail-CLOSED by contract: anything it cannot complete raises ``PraxisUnreachable`` and
every Stop gate treats that as a BLOCK. That is right. What was wrong is what counted as "cannot
complete": a 10-second timeout with NO retry. Praxis runs on App Runner and scales to zero, so the
first request after an idle period waits for a container to boot — and the client called that
PRAXIS UNREACHABLE and blocked every gate on the box, twice, against a perfectly healthy service
that was merely asleep.

The other half is the opposite mistake: a 403 (an API key not scoped to the org) is a definite
ANSWER. Retrying it just gets the same answer more slowly while burning the retry budget and, at
the driver level, incrementing an outage counter that halts the run for the wrong reason.

So the client has to separate two questions — is repeating this USEFUL, and is repeating it SAFE —
and these tests pin both, plus the case where conflating them corrupts state: a POST that timed out
may already have been applied, and re-sending it would turn one lease claim into two.
"""

from __future__ import annotations

import http.server
import json
import threading
from contextlib import contextmanager

import pytest

from agent_factory import ingestion_api  # noqa: F401  -- canonicalizes the hooks modules first

import _praxis  # noqa: E402


@contextmanager
def serve(handler_fn):
    """A throwaway HTTP server. ``handler_fn(n, method)`` returns (status, body) for request n."""
    seen: list[tuple[str, str]] = []

    class H(http.server.BaseHTTPRequestHandler):
        def _go(self):
            seen.append((self.command, self.path))
            status, body = handler_fn(len(seen), self.command)
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = _go

        def log_message(self, *a):  # keep pytest output clean
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}", seen
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def fast_retries(monkeypatch):
    """Exercise the retry PATH without sleeping through its production backoff."""
    monkeypatch.setattr(_praxis, "_HTTP_BACKOFF_S", 0.01)
    monkeypatch.setattr(_praxis, "_HTTP_ATTEMPTS", 4)
    monkeypatch.setenv("PRAXIS_AUTH_DISABLED", "1")


def test_a_cold_start_is_waited_out_not_reported_as_an_outage(fast_retries, monkeypatch):
    """THE REGRESSION: App Runner answers 503 while it wakes. That used to block every gate."""
    def scripted(n, method):
        return (503, {"detail": "service unavailable"}) if n <= 2 else (200, {"ok": True})

    with serve(scripted) as (base, seen):
        monkeypatch.setenv("PRAXIS_API_BASE_URL", base)
        assert _praxis._request("GET", "/facts/by") == {"ok": True}
    assert len(seen) == 3, "expected two retries then success"


def test_a_403_is_an_answer_and_is_never_retried(fast_retries, monkeypatch):
    """'API key is not scoped to org X' will not become true on the fourth attempt."""
    with serve(lambda n, method: (403, {"detail": "API key is not scoped to org 'x'"})) as (base, seen):
        monkeypatch.setenv("PRAXIS_API_BASE_URL", base)
        with pytest.raises(_praxis.PraxisUnreachable) as err:
            _praxis._request("GET", "/facts/by")
    assert len(seen) == 1, f"a definite answer must not spend the retry budget (sent {len(seen)})"
    assert "403" in str(err.value)


def test_a_404_is_an_answer_and_is_never_retried(fast_retries, monkeypatch):
    with serve(lambda n, method: (404, {"detail": "unknown space"})) as (base, seen):
        monkeypatch.setenv("PRAXIS_API_BASE_URL", base)
        with pytest.raises(_praxis.PraxisUnreachable):
            _praxis._request("GET", "/facts/by")
    assert len(seen) == 1


def test_an_opted_in_404_short_circuits_without_burning_a_backoff(fast_retries, monkeypatch):
    with serve(lambda n, method: (404, {})) as (base, seen):
        monkeypatch.setenv("PRAXIS_API_BASE_URL", base)
        assert _praxis._request("GET", "/checks", not_found_ok=True) == {}
    assert len(seen) == 1


def test_a_409_lease_conflict_stays_a_conflict_and_is_never_retried(fast_retries, monkeypatch):
    """A live owner holds the lease. Repeating the claim cannot change that, and the caller
    distinguishes PraxisConflict from an outage by TYPE — so the type has to survive the retry
    loop."""
    with serve(lambda n, method: (409, {"detail": "held"})) as (base, seen):
        monkeypatch.setenv("PRAXIS_API_BASE_URL", base)
        with pytest.raises(_praxis.PraxisConflict):
            _praxis._request("POST", "/requirements/c1/claim", body={"owner": "w"})
    assert len(seen) == 1


def test_a_write_is_retried_when_the_gateway_proves_it_never_landed(fast_retries, monkeypatch):
    """502/503/504 come from the proxy: the app never saw the request, so repeating is safe."""
    def scripted(n, method):
        return (503, {"detail": "waking"}) if n == 1 else (200, {"claimed": True})

    with serve(scripted) as (base, seen):
        monkeypatch.setenv("PRAXIS_API_BASE_URL", base)
        assert _praxis._request("POST", "/requirements/c1/claim", body={"owner": "w"})["claimed"]
    assert len(seen) == 2


def test_a_timed_out_write_is_not_repeated(fast_retries, monkeypatch):
    """The one case where retrying corrupts state.

    urllib cannot tell a CONNECT timeout from a READ timeout, so a POST that timed out may already
    have been applied server-side. Re-sending it would claim the same lease twice. Reads have no
    such hazard, so they still retry — the split is on the METHOD, not on optimism.
    """
    def never_answers(n, method):
        raise TimeoutError("read timeout")

    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise TimeoutError("timed out")

    monkeypatch.setattr(_praxis.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("PRAXIS_API_BASE_URL", "http://127.0.0.1:1")

    with pytest.raises(_praxis.PraxisUnreachable):
        _praxis._request("POST", "/requirements/c1/claim", body={"owner": "w"})
    assert calls["n"] == 1, "a timed-out write must not be repeated — it may already have applied"

    calls["n"] = 0
    with pytest.raises(_praxis.PraxisUnreachable):
        _praxis._request("GET", "/facts/by")
    assert calls["n"] == _praxis._HTTP_ATTEMPTS, "a read has no such hazard and must retry"


def test_the_timeout_is_generous_enough_for_a_cold_start():
    """10s was the number that failed. A scale-to-zero boot routinely exceeds it."""
    assert _praxis._HTTP_TIMEOUT_S >= 30
    assert _praxis._HTTP_ATTEMPTS >= 2
