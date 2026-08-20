"""Small cross-process advisory lock for registry read-modify-write transactions.

Assumptions, deliberately narrow:

* ``flock`` is advisory and POSIX-local.  It is reliable on a local filesystem and is
  NOT safe over NFS (or any network filesystem whose ``flock`` is emulated or a no-op);
  registry files must live on local disk.
* The lock has no timeout and no deadlock detection: a caller blocks until the holder
  releases.  That is intentional -- these critical sections are a load, an in-memory
  mutation, and an atomic rename -- but a wedged holder wedges every writer.
"""


from __future__ import annotations

from contextlib import contextmanager
import fcntl
from pathlib import Path
from typing import Iterator


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
