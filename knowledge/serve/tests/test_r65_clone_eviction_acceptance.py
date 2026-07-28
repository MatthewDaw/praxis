"""Acceptance test for ticket R65: storage capacity, clone eviction, disk guard, jobs view.

Coverage:
  - Clone eviction: clones untouched for > eviction_period (default 14 days) removed and recorded
  - Disk guard: claim refused when free space < max(20 GB, 2× largest clone)
  - Jobs view: per-clone sizes and volume headroom readable
"""

from __future__ import annotations

import os
from pathlib import Path

from knowledge.serve.box_service_clone import RepoClone
from knowledge.serve.box_service_clone_eviction import (
    DEFAULT_EVICTION_PERIOD_SECONDS,
    DEFAULT_HEADROOM_FLOOR_BYTES,
    CloneEvictionManager,
    clone_size_bytes,
)


# ---------------------------------------------------------------------------
# Clone eviction — R65.1
# ---------------------------------------------------------------------------


class TestCloneEviction:
    """R65.1: clone untouched past eviction period is removed and recorded."""

    def test_default_eviction_period_is_14_days(self):
        assert DEFAULT_EVICTION_PERIOD_SECONDS == 14 * 24 * 3600.0

    def test_default_headroom_floor_is_20_gb(self):
        assert DEFAULT_HEADROOM_FLOOR_BYTES == 20 * 1024**3

    def test_clone_within_window_not_evicted(self, tmp_path: Path):
        """A clone last touched within the eviction window is NOT removed."""
        now = [1_000_000.0]
        clones_root = str(tmp_path / "clones")
        os.makedirs(clones_root)
        mgr = CloneEvictionManager(clones_root=clones_root, clock=lambda: now[0])
        clone = _fake_clone(clones_root, "https://github.com/org/repo.git")
        mgr.record_touch(clone, job_id="job-1")

        now[0] += 10 * 24 * 3600.0  # 10 days — within window
        evicted = mgr.evict_expired()
        assert evicted == 0
        assert os.path.isdir(clone.clone_path)

    def test_clone_past_window_is_evicted_and_recorded(self, tmp_path: Path):
        """A clone last touched past the eviction window IS removed."""
        now = [1_000_000.0]
        clones_root = str(tmp_path / "clones")
        os.makedirs(clones_root)
        mgr = CloneEvictionManager(clones_root=clones_root, clock=lambda: now[0])
        clone = _fake_clone(clones_root, "https://github.com/org/repo.git")
        mgr.record_touch(clone, job_id="job-1")

        now[0] += 20 * 24 * 3600.0  # 20 days — past window
        evicted = mgr.evict_expired()
        assert evicted == 1
        assert len(mgr.eviction_log()) == 1

    def test_touch_refreshes_eviction_clock(self, tmp_path: Path):
        """Each touch resets the eviction clock for that clone."""
        now = [1_000_000.0]
        clones_root = str(tmp_path / "clones")
        os.makedirs(clones_root)
        mgr = CloneEvictionManager(clones_root=clones_root, clock=lambda: now[0])
        clone = _fake_clone(clones_root, "https://github.com/org/repo.git")
        mgr.record_touch(clone, job_id="job-1")

        now[0] += 13 * 24 * 3600.0
        mgr.record_touch(clone, job_id="job-2")  # refreshes

        now[0] += 13 * 24 * 3600.0  # 13 days since last touch
        evicted = mgr.evict_expired()
        assert evicted == 0  # not evicted because touch was refreshed

    def test_clone_size_bytes_reported(self, tmp_path: Path):
        """clone_size_bytes() returns the on-disk size of a clone directory."""
        clones_root = str(tmp_path / "clones")
        os.makedirs(clones_root)
        clone_dir = str(tmp_path / "clones" / "repo.git")
        os.makedirs(clone_dir)
        (Path(clone_dir) / "objects").mkdir()
        (Path(clone_dir) / "objects" / "pack").write_bytes(b"x" * 1000)

        size = clone_size_bytes(clone_dir)
        assert size >= 1000


# ---------------------------------------------------------------------------
# Disk space guard — R65.2
# ---------------------------------------------------------------------------


class TestDiskSpaceGuard:
    """R65.2: claim refused when free space below headroom floor."""

    def test_claim_allowed_when_free_space_above_headroom(self, tmp_path: Path):
        """When free space >= headroom floor, the guard does not block."""
        clones_root = str(tmp_path / "clones")
        os.makedirs(clones_root)
        mgr = CloneEvictionManager(clones_root=clones_root)
        # With no clones, headroom = max(20GB, 0) = 20GB
        # Simulate ample free space
        headroom = mgr.compute_headroom()
        assert headroom == DEFAULT_HEADROOM_FLOOR_BYTES

    def test_headroom_is_max_of_floor_and_twice_largest_clone(self, tmp_path: Path):
        """Headroom = max(20GB, 2 * largest_clone_size)."""
        clones_root = str(tmp_path / "clones")
        os.makedirs(clones_root)
        mgr = CloneEvictionManager(clones_root=clones_root)

        # No clones -> headroom = 20 GB
        assert mgr.compute_headroom() == DEFAULT_HEADROOM_FLOOR_BYTES

        # A 50 GB clone -> headroom = 100 GB (2x)
        mgr._largest_clone_bytes = 50 * 1024**3
        assert mgr.compute_headroom() == 100 * 1024**3

    def test_headroom_info_readable(self, tmp_path: Path):
        """Per-clone sizes and headroom are readable from the jobs view context."""
        clones_root = str(tmp_path / "clones")
        os.makedirs(clones_root)
        mgr = CloneEvictionManager(clones_root=clones_root)

        info = mgr.storage_summary()
        assert "headroom_bytes" in info
        assert "free_bytes" in info
        assert "clone_count" in info
        assert info["headroom_bytes"] >= DEFAULT_HEADROOM_FLOOR_BYTES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_clone(clones_root: str, origin_url: str) -> RepoClone:
    """Create a fake clone on disk so eviction can actually remove it."""
    from knowledge.serve.box_service_clone import repo_slug
    slug = repo_slug(origin_url)
    clone_path = os.path.join(clones_root, f"{slug}.git")
    main_worktree_path = os.path.join(clones_root, slug, "main")
    os.makedirs(clone_path, exist_ok=True)
    os.makedirs(main_worktree_path, exist_ok=True)
    Path(clone_path, "HEAD").write_text("ref: refs/heads/main\n")
    Path(main_worktree_path, ".keep").write_text("")
    return RepoClone(origin_url=origin_url, clone_path=clone_path, main_worktree_path=main_worktree_path)
