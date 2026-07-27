"""On-disk hook trail per job (R66): each running job's hook events are written
to a job-scoped on-disk file under a stated byte cap (64 MB) with rotation, so
the trail is readable during the job's lifetime and is deleted together with the
job worktree after the final flush — while the persisted activity tail
(``box_service_activity_tail.ActivityTailStore``) remains readable after the
session is reaped.

Distinct from ``ActivityTailStore`` (in-memory, 8 KB cap, survives reap): the
hook trail is an on-disk append-only log of ALL hook events during execution,
capped at 64 MB so a stuck or runaway session never fills the disk. The
activity tail is a smaller bounded window of recent messages that persists
past reap; the hook trail is disposable — it is deleted when the job's on-disk
state is torn down.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from knowledge.serve.box_service_models import Job

#: Default byte cap for a job's on-disk hook trail (64 MB, R66).
DEFAULT_HOOK_TRAIL_BYTE_CAP = 64 * 1024 * 1024

#: Default filename under the job-scoped directory.
HOOK_TRAIL_FILENAME = "hook-trail.log"


def _ensure_dir(path: str) -> None:
    """Ensure the directory for ``path`` exists."""
    os.makedirs(os.path.dirname(path), exist_ok=True)


class HookTrailManager:
    """Manages on-disk hook trail files, one per job, under ``jobs_root``.

    Each job's trail is written to ``<jobs_root>/<job.id>/hook-trail.log``,
    so concurrent jobs never share a trail file. The file is capped at
    ``byte_cap`` bytes: when an append would exceed the cap, the oldest bytes
    are rotated out so total on-disk bytes per job never exceed the cap.

    ``clock`` is injectable for deterministic testing.
    """

    def __init__(
        self,
        *,
        jobs_root: str,
        byte_cap: int = DEFAULT_HOOK_TRAIL_BYTE_CAP,
        clock: Callable[[], float] | None = None,
    ) -> None:
        import time
        self._jobs_root = jobs_root
        self._byte_cap = byte_cap
        self._clock = clock or time.time

    def path_for(self, job: Job) -> str:
        """The job-scoped path for ``job``'s hook trail file."""
        return os.path.join(self._jobs_root, job.id, HOOK_TRAIL_FILENAME)

    def append(self, job: Job, chunk: str) -> str:
        """Append ``chunk`` to ``job``'s on-disk hook trail, rotating out the
        oldest bytes once the byte cap is exceeded. Returns the trail path.

        Creates the job-scoped directory on first write.
        """
        path = self.path_for(job)
        _ensure_dir(path)
        existing = b""
        if os.path.exists(path):
            with open(path, "rb") as fh:
                existing = fh.read()
        content = existing + chunk.encode("utf-8")
        if len(content) > self._byte_cap:
            content = content[-self._byte_cap :]  # rotate out oldest bytes
        with open(path, "wb") as fh:
            fh.write(content)
        return path

    def read(self, job: Job) -> str:
        """Read ``job``'s current on-disk hook trail. Returns the empty string
        if no trail has been written yet."""
        path = self.path_for(job)
        if not os.path.exists(path):
            return ""
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", errors="replace")

    def delete(self, job: Job) -> bool:
        """Delete ``job``'s hook trail file and its job-scoped directory if
        empty. Returns ``True`` if a file was deleted, ``False`` if there was
        nothing to delete."""
        path = self.path_for(job)
        if not os.path.exists(path):
            return False
        os.remove(path)
        # Remove the job-scoped directory if empty
        job_dir = os.path.dirname(path)
        try:
            os.rmdir(job_dir)
        except OSError:
            pass  # directory not empty or already gone
        return True

    def delete_for_jobs(self, job_ids: list[str]) -> int:
        """Delete hook trail files for the given ``job_ids``. Returns the
        count deleted."""
        deleted = 0
        for job_id in job_ids:
            job_dir = os.path.join(self._jobs_root, job_id)
            trail_path = os.path.join(job_dir, HOOK_TRAIL_FILENAME)
            if os.path.isfile(trail_path):
                os.remove(trail_path)
                deleted += 1
                try:
                    os.rmdir(job_dir)
                except OSError:
                    pass
        return deleted
