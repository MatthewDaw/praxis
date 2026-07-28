"""Acceptance test for ticket R66: box-side hook trail, storage, cleanup, lifecycle.

Coverage:
  - Hook trail written to job-scoped on-disk path with 64 MB byte cap and rotation
  - Reaped job: hook trail file deleted, session on-disk state gone, persisted tail readable
  - Observation events purged past 90-day retention window
  - Deleting project space cascades its job rows and observation events
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


from knowledge.serve.box_service_activity_tail import ActivityTailStore
from knowledge.serve.box_service_hook_trail import (
    DEFAULT_HOOK_TRAIL_BYTE_CAP,
    HookTrailManager,
)
from knowledge.serve.box_service_models import Job, JobState
from knowledge.serve.box_service_store import JobStore
from knowledge.serve.box_service_worktree_cleanup import (
    reap_and_cleanup,
)

# ---------------------------------------------------------------------------
# Hook trail — R66.1
# ---------------------------------------------------------------------------


def _make_job(
    job_id: str = "job-1",
    project: str = "proj-a",
    state: JobState = JobState.RUNNING,
) -> Job:
    return Job(
        id=job_id,
        project=project,
        snapshot=f"prd-{project}",
        state=state,
        run_owner="box-1",
        org="org-a",
    )


class TestHookTrailOnDisk:
    """R66.1: hook trail is written to a job-scoped path, capped at 64 MB, with rotation."""

    def test_trail_written_to_job_scoped_path(self, tmp_path: Path):
        """The hook trail file lives under a job-scoped directory so concurrent
        jobs never share a trail file."""
        mgr = HookTrailManager(jobs_root=str(tmp_path / "trails"))
        job_a = _make_job("job-a")
        job_b = _make_job("job-b")

        mgr.append(job_a, "a-event-1\n")
        mgr.append(job_b, "b-event-1\n")

        path_a = mgr.path_for(job_a)
        path_b = mgr.path_for(job_b)
        assert path_a != path_b
        assert Path(path_a).exists()
        assert Path(path_b).exists()

    def test_byte_cap_and_rotation(self, tmp_path: Path):
        """When appended content exceeds the cap, oldest bytes rotate out."""
        cap = 100
        mgr = HookTrailManager(jobs_root=str(tmp_path / "trails"), byte_cap=cap)
        job = _make_job()

        # Fill to just under cap
        under_cap = "x" * 90
        mgr.append(job, under_cap)
        content = mgr.read(job)
        assert len(content.encode("utf-8")) <= cap

        # Append more — rotation should kick in
        mgr.append(job, "y" * 30)
        content = mgr.read(job)
        assert len(content.encode("utf-8")) <= cap
        # Most recent bytes are retained
        assert content.endswith("y" * 30)

    def test_default_cap_is_64_mb(self):
        """The stated byte cap is 64 MB (the acceptance condition's explicit number)."""
        assert DEFAULT_HOOK_TRAIL_BYTE_CAP == 64 * 1024 * 1024

    def test_ten_times_cap_load_stays_bounded(self, tmp_path: Path):
        """Under a load generating ten times the cap, total on-disk trail bytes
        per job never exceed the cap."""
        cap = 1024  # small cap so the test is fast
        mgr = HookTrailManager(jobs_root=str(tmp_path / "trails"), byte_cap=cap)
        job = _make_job()

        # Push 10x the cap in chunks
        ten_x = cap * 10
        chunk = "abcdefghijklmnopqrstuvwxyz\n"  # 27 bytes
        written = 0
        while written < ten_x:
            mgr.append(job, chunk)
            written += len(chunk.encode("utf-8"))

        trail_path = mgr.path_for(job)
        on_disk = os.path.getsize(trail_path)
        assert on_disk <= cap, f"on-disk {on_disk} exceeds cap {cap}"

    def test_trail_survives_append_after_trim(self, tmp_path: Path):
        """A trail that has been rotated is still appendable and readable."""
        mgr = HookTrailManager(jobs_root=str(tmp_path / "trails"), byte_cap=50)
        job = _make_job()
        mgr.append(job, "a" * 60)
        mgr.append(job, "final\n")
        content = mgr.read(job)
        assert "final" in content


# ---------------------------------------------------------------------------
# Cleanup — R66.2
# ---------------------------------------------------------------------------


class TestReapedJobCleanup:
    """R66.2: reaped job's hook trail and on-disk state are deleted, persisted tail readable."""

    def test_reap_deletes_hook_trail(self, tmp_path: Path):
        """After reap_and_cleanup, the hook trail file is gone."""
        clone_path = str(tmp_path / "clone")
        worktree_path = str(tmp_path / "clone" / "jobs" / "job-1")
        trails_root = str(tmp_path / "trails")
        os.makedirs(worktree_path, exist_ok=True)

        mgr = HookTrailManager(jobs_root=trails_root)
        job = _make_job()
        job.worktree_path = worktree_path  # set worktree so cleanup proceeds
        mgr.append(job, "some hook event\n")
        trail_path = mgr.path_for(job)
        assert Path(trail_path).exists()

        tail_store = ActivityTailStore()
        tail_store.append(job, "final output\n")

        result = reap_and_cleanup(
            job,
            final_tail_chunk="terminal\n",
            tail_store=tail_store,
            terminal_state=JobState.COMPLETED,
            terminal_reason="merged",
            merged=True,
            clone_path=clone_path,
            hook_trail_manager=mgr,
            runner=lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        )
        # reap_and_cleanup returns cleanup_job_worktree's result; if worktree_path is None
        # it returns False. With a worktree set and merged+tail_persisted, it returns True.
        assert result is True
        assert not Path(trail_path).exists()
        assert job.worktree_path is None

    def test_persisted_tail_readable_after_cleanup(self, tmp_path: Path):
        """The persisted activity tail remains readable after hook trail deletion."""
        mgr = HookTrailManager(jobs_root=str(tmp_path / "trails"))
        job = _make_job()
        mgr.append(job, "hook event\n")
        tail_store = ActivityTailStore()
        tail_store.append(job, "persisted activity\n")

        reap_and_cleanup(
            job,
            final_tail_chunk="terminal\n",
            tail_store=tail_store,
            terminal_state=JobState.COMPLETED,
            terminal_reason="merged",
            merged=True,
            clone_path=str(tmp_path / "clone"),
            hook_trail_manager=mgr,
            runner=lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        )

        from knowledge.serve.job_authz import JobPrincipal, PrincipalKind
        principal = JobPrincipal(kind=PrincipalKind.OPERATOR, id="op-1", org_id="org-a")
        assert "persisted activity" in tail_store.read_stored(job, principal)


# ---------------------------------------------------------------------------
# Retention — R66.3
# ---------------------------------------------------------------------------


class TestObservationEventRetention:
    """R66.3: observation events older than 90 days are archived/purged, absent from queries."""

    def test_events_past_90_days_purged(self):
        """Events older than the 90-day retention window are purged."""
        now = [1_000_000.0]
        store = ActivityTailStore(clock=lambda: now[0])
        job = _make_job()
        store.append(job, "old event")
        assert store.read_stored(job, _principal_for_job(job)) == "old event"

        # Advance past 90 days
        ninety_days = 90 * 24 * 3600.0
        now[0] += ninety_days + 1.0

        purged = store.purge_expired(retention_seconds=ninety_days)
        assert purged == 1
        assert store.read_stored(job, _principal_for_job(job)) == ""

    def test_events_within_window_survive(self):
        """Events within the retention window are not purged."""
        now = [1_000_000.0]
        store = ActivityTailStore(clock=lambda: now[0])
        job = _make_job()
        store.append(job, "recent event")

        ninety_days = 90 * 24 * 3600.0
        now[0] += ninety_days - 1000.0  # just inside the window
        purged = store.purge_expired(retention_seconds=ninety_days)
        assert purged == 0
        assert store.read_stored(job, _principal_for_job(job)) == "recent event"


# ---------------------------------------------------------------------------
# Cascading delete — R66.4
# ---------------------------------------------------------------------------


class TestProjectCascadeDelete:
    """R66.4: deleting a project space cascades its job rows and observation events."""

    def test_delete_project_removes_job_rows(self):
        """JobStore.delete_project() removes every job for that project."""
        store = JobStore()
        job_a = store.create(project="proj-a", snapshot="prd-proj-a")
        job_b = store.create(project="proj-b", snapshot="prd-proj-b")
        job_a2 = store.create(project="proj-a", snapshot="prd-proj-a")

        deleted = store.delete_project("proj-a")
        assert deleted == 2
        assert store.get(job_a.id) is None
        assert store.get(job_a2.id) is None
        assert store.get(job_b.id) is not None  # different project untouched

    def test_delete_project_cascades_activity_tails(self):
        """ActivityTailStore.delete_project() cascades its entries."""
        store = ActivityTailStore()
        job_a = _make_job("job-1", project="proj-a")
        job_b = _make_job("job-2", project="proj-b")
        store.append(job_a, "a's activity")
        store.append(job_b, "b's activity")

        deleted = store.delete_project("proj-a")
        assert deleted == 1
        assert store.read_stored(job_a, _principal_for_job(job_a)) == ""
        assert store.read_stored(job_b, _principal_for_job(job_b)) == "b's activity"

    def test_delete_for_jobs_cascades_hook_trails(self, tmp_path: Path):
        """HookTrailManager.delete_for_jobs() removes all hook trail files for given job ids."""
        mgr = HookTrailManager(jobs_root=str(tmp_path / "trails"))
        job_a = _make_job("job-a", project="proj-a")
        job_b = _make_job("job-b", project="proj-b")
        mgr.append(job_a, "a's hook events\n")
        mgr.append(job_b, "b's hook events\n")

        deleted = mgr.delete_for_jobs(["job-a"])
        assert deleted == 1
        assert not Path(mgr.path_for(job_a)).exists()
        assert Path(mgr.path_for(job_b)).exists()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _principal_for_job(job: Job):
    from knowledge.serve.job_authz import JobPrincipal, PrincipalKind
    return JobPrincipal(kind=PrincipalKind.OPERATOR, id="op-1", org_id=job.org)
