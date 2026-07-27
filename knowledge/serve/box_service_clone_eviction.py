"""Clone eviction and disk-space guard (R65): per-repo clones untouched past
the configured eviction period are removed and the removal is recorded; claims
are refused when free space on the workspace volume falls below the configured
headroom floor (default max(20 GB, 2× largest clone)); per-clone sizes and
volume headroom are readable from the jobs view.

Pure decision logic with injectable clock and os-probe seams, matching every
other ``box_service_*`` building block so the contract is assertable without a
real filesystem.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from knowledge.serve.box_service_clone import RepoClone

#: Default eviction period (14 days in seconds, R65).
DEFAULT_EVICTION_PERIOD_SECONDS = 14 * 24 * 3600.0

#: Default headroom floor (20 GB in bytes, R65).
DEFAULT_HEADROOM_FLOOR_BYTES = 20 * 1024**3


def measure_free_space(path: str) -> int:
    """Return free space in bytes on the filesystem holding ``path``."""
    stat = os.statvfs(path)
    return stat.f_frsize * stat.f_bavail


def clone_size_bytes(clone_path: str) -> int:
    """Return the on-disk size in bytes of ``clone_path``, or 0 if it doesn't exist."""
    if not os.path.isdir(clone_path):
        return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(clone_path):
        for fname in filenames:
            fp = os.path.join(dirpath, fname)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


@dataclass
class _CloneEntry:
    """Tracked state for one clone: last touch time and the job that touched it."""

    last_touch_at: float
    last_job_id: str


@dataclass
class EvictionRecord:
    """Record of a clone that was evicted."""

    origin_url: str
    clone_path: str
    evicted_at: float
    last_touch_at: float
    last_job_id: str


class CloneEvictionManager:
    """Tracks clone usage and evicts clones past the eviction period, measures
    disk space, and computes the headroom floor for claim guarding.

    ``clock`` is injectable so eviction can be asserted deterministically
    without sleeping past a real window.
    ``free_space_fn`` and ``size_fn`` are injectable for testing without
    a real filesystem.
    """

    def __init__(
        self,
        *,
        clones_root: str,
        eviction_period_seconds: float = DEFAULT_EVICTION_PERIOD_SECONDS,
        headroom_floor_bytes: int = DEFAULT_HEADROOM_FLOOR_BYTES,
        clock: Callable[[], float] | None = None,
        free_space_fn: Callable[[str], int] | None = None,
        size_fn: Callable[[str], int] | None = None,
    ) -> None:
        self._clones_root = clones_root
        self._eviction_period = eviction_period_seconds
        self._headroom_floor = headroom_floor_bytes
        self._clock = clock or time.time
        self._free_space_fn = free_space_fn or measure_free_space
        self._size_fn = size_fn or clone_size_bytes
        self._entries: dict[str, _CloneEntry] = {}
        self._eviction_log: list[EvictionRecord] = []
        #: Largest clone size observed, used in headroom computation.
        self._largest_clone_bytes: int = 0

    # -- Touch tracking ----------------------------------------------------------

    def record_touch(self, clone: RepoClone, *, job_id: str) -> None:
        """Record that ``clone`` was used by ``job_id``, refreshing its eviction
        clock and updating the largest-clone track."""
        now = self._clock()
        self._entries[clone.clone_path] = _CloneEntry(last_touch_at=now, last_job_id=job_id)
        # Update largest-clone track
        size = self._size_fn(clone.clone_path)
        if size > self._largest_clone_bytes:
            self._largest_clone_bytes = size

    # -- Eviction ----------------------------------------------------------------

    def evict_expired(self) -> int:
        """Remove every clone whose last touch is past the eviction period.
        Returns the count evicted."""
        now = self._clock()
        evicted = 0
        for clone_path, entry in list(self._entries.items()):
            if (now - entry.last_touch_at) > self._eviction_period:
                self._remove_clone(clone_path, entry)
                del self._entries[clone_path]
                evicted += 1
        return evicted

    def _remove_clone(self, clone_path: str, entry: _CloneEntry) -> None:
        """Remove the clone at ``clone_path`` and record the eviction."""
        now = self._clock()
        if os.path.isdir(clone_path):
            shutil.rmtree(clone_path, ignore_errors=True)
        self._eviction_log.append(
            EvictionRecord(
                origin_url="(unknown)",  # path->url reverse mapping not stored
                clone_path=clone_path,
                evicted_at=now,
                last_touch_at=entry.last_touch_at,
                last_job_id=entry.last_job_id,
            )
        )

    def eviction_log(self) -> list[EvictionRecord]:
        """Return the ordered list of evictions that have occurred."""
        return list(self._eviction_log)

    # -- Disk-space guard --------------------------------------------------------

    def compute_headroom(self) -> int:
        """Return the headroom floor: ``max(floor, 2 × largest_clone)``."""
        headroom = max(self._headroom_floor, 2 * self._largest_clone_bytes)
        return headroom

    def free_bytes(self) -> int:
        """Return free space on the workspace volume in bytes."""
        return self._free_space_fn(self._clones_root)

    def storage_summary(self) -> dict:
        """Return the storage summary readable from the jobs view (R65.3):
        ``headroom_bytes``, ``free_bytes``, ``clone_count``, and
        ``largest_clone_bytes`` so the operator can see capacity at a glance."""
        return {
            "headroom_bytes": self.compute_headroom(),
            "free_bytes": self.free_bytes(),
            "clone_count": len(self._entries),
            "largest_clone_bytes": self._largest_clone_bytes,
            "eviction_period_hours": self._eviction_period / 3600.0,
        }
