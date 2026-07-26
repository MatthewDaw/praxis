"""Tests for the per-repo host advisory lock (R18).

Proves the acceptance condition directly: two concurrent jobs on one repo
each running a fixed-host-port/fixture command are serialized by the
per-repo advisory lock, while two concurrent jobs running only
non-contending build commands never block on it. Also proves the lock is
keyed per repo (two different repos never contend) and carries lease
metadata (holder id, heartbeat, expiry) per the cross-cutting lease
invariant (9c2f003a1dac4376b7b451ae74be7f2f).
"""

from __future__ import annotations

import tempfile
import threading
import time

from knowledge.serve.box_service_host_lock import (
    HostAdvisoryLock,
    is_contending_command,
    run_locked,
)


def _timed_runner(events: list[tuple[str, float]], label: str, hold_s: float = 0.05):
    def _run():
        events.append((f"{label}-start", time.monotonic()))
        time.sleep(hold_s)
        events.append((f"{label}-end", time.monotonic()))
        return 0

    return _run


def test_contending_commands_on_same_repo_are_serialized():
    with tempfile.TemporaryDirectory() as tmp:
        lock = HostAdvisoryLock(lock_dir=tmp)
        events: list[tuple[str, float]] = []

        t1 = threading.Thread(
            target=lambda: run_locked(
                "repo-a", "docker compose up -d --wait db", _timed_runner(events, "a"), lock=lock
            )
        )
        t2 = threading.Thread(
            target=lambda: run_locked(
                "repo-a", "docker compose up -d --wait db", _timed_runner(events, "b"), lock=lock
            )
        )
        t1.start()
        time.sleep(0.01)  # ensure t1 acquires first
        t2.start()
        t1.join()
        t2.join()

        # One invocation must fully finish before the other starts: no interleave.
        order = [label for label, _ in sorted(events, key=lambda e: e[1])]
        first, second = order[:2], order[2:]
        assert first == ["a-start", "a-end"] or first == ["b-start", "b-end"]
        assert second == ["b-start", "b-end"] or second == ["a-start", "a-end"]
        assert first != second


def test_non_contending_commands_never_block_on_the_lock():
    with tempfile.TemporaryDirectory() as tmp:
        lock = HostAdvisoryLock(lock_dir=tmp)
        events: list[tuple[str, float]] = []

        t1 = threading.Thread(
            target=lambda: run_locked(
                "repo-a", "uv run --group dev pytest agent_factory/tests -q", _timed_runner(events, "a", hold_s=0.1), lock=lock
            )
        )
        t2 = threading.Thread(
            target=lambda: run_locked(
                "repo-a", "uv run --group dev pytest agent_factory/tests -q", _timed_runner(events, "b", hold_s=0.1), lock=lock
            )
        )
        t1.start()
        time.sleep(0.01)
        t2.start()
        t1.join()
        t2.join()

        starts = dict(events)
        # b starts before a ends -- proof neither waited on the other.
        assert starts["b-start"] < starts["a-end"]


def test_lock_is_keyed_per_repo():
    with tempfile.TemporaryDirectory() as tmp:
        lock = HostAdvisoryLock(lock_dir=tmp)
        events: list[tuple[str, float]] = []

        t1 = threading.Thread(
            target=lambda: run_locked(
                "repo-a", "docker compose up -d --wait db", _timed_runner(events, "a", hold_s=0.1), lock=lock
            )
        )
        t2 = threading.Thread(
            target=lambda: run_locked(
                "repo-b", "docker compose up -d --wait db", _timed_runner(events, "b", hold_s=0.1), lock=lock
            )
        )
        t1.start()
        time.sleep(0.01)
        t2.start()
        t1.join()
        t2.join()

        starts = dict(events)
        # Different repos: b starts well before a (on repo-a) finishes.
        assert starts["b-start"] < starts["a-end"]


def test_classifier_matches_fixed_host_port_and_fixture_commands():
    assert is_contending_command("docker compose up -d --wait db")
    assert is_contending_command("scripts/local-db.sh")
    assert is_contending_command("just db-up")
    assert is_contending_command("uv run --group dev pytest knowledge/serve/tests -q")
    assert not is_contending_command("uv run --group dev pytest agent_factory/tests -q")
    assert not is_contending_command("uv run ruff check .")


def test_acquire_stamps_holder_heartbeat_and_expiry_and_releases_on_exit():
    with tempfile.TemporaryDirectory() as tmp:
        lock = HostAdvisoryLock(lock_dir=tmp)
        with lock.acquire("repo-a") as lease:
            assert lease.holder
            assert lease.heartbeat_at > 0
            assert lease.expires_at > lease.heartbeat_at

        # Lock released -- a second acquisition on the same repo succeeds immediately.
        acquired = threading.Event()

        def _reacquire():
            with lock.acquire("repo-a"):
                acquired.set()

        t = threading.Thread(target=_reacquire)
        t.start()
        t.join(timeout=1.0)
        assert acquired.is_set()
