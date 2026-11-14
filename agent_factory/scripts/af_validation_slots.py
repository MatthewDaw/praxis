"""Admission for bounded isolated API checks, or one exclusive browser/build check.

The original global lock remains the gate, so older exclusive callers stay safe.
Returned descriptors must survive exec and remain open for the command's lifetime.
"""
import fcntl
import hashlib
import os
from pathlib import Path
import time


def acquire(state_dir, *, integration=False, cwd=None, slots=2):
    if slots not in (1, 2, 3):
        raise ValueError('isolated integration concurrency must be one, two, or three')
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    descriptors = []
    def open_lock(name):
        fd = os.open(state / name, os.O_CREAT | os.O_RDWR, 0o600)
        descriptors.append(fd)
        return fd
    try:
        # A single worktree must not concurrently regenerate/build the same files.
        if cwd is not None:
            name = hashlib.sha256(os.fsencode(Path(cwd).resolve())).hexdigest()
            fcntl.flock(open_lock('worktree-' + name + '.lock'), fcntl.LOCK_EX)
        gate = open_lock('heavy-validation.lock')
        fcntl.flock(gate, fcntl.LOCK_SH if integration else fcntl.LOCK_EX)
        if integration:
            slot_fds = [open_lock(f'integration-validation-{i}.lock') for i in range(slots)]
            selected = None
            while selected is None:
                for fd in slot_fds:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    selected = fd
                    break
                if selected is None:
                    time.sleep(.1)
            for fd in slot_fds:
                if fd != selected:
                    os.close(fd)
                    descriptors.remove(fd)
        for fd in descriptors:
            os.set_inheritable(fd, True)
        return descriptors
    except BaseException:
        for fd in descriptors:
            os.close(fd)
        raise
