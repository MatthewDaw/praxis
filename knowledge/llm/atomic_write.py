"""Atomically write text to a path: write a temp sibling, then ``os.replace``.

Replacing the live file via a rename (atomic on one filesystem) avoids ever
truncating it in place. On Windows that dodges an ``EINVAL`` race where an external
scanner (Defender / Search indexer) grabs the file between a network-paced cache
loop's writes and collides with the next truncating ``open(path, "w")``. It's also
crash-safe: a reader never sees a half-written cache.
"""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp sibling + atomic rename (utf-8)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # PID-suffixed temp so separate worker processes sharing this path don't collide;
    # within a process the callers already serialize on a file lock.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
