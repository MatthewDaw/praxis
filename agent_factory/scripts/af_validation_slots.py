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
        # Hold a turnstile only during admission. Once an exclusive build queues,
        # later API checks cannot continually acquire shared access ahead of it.
        turnstile = open_lock('validation-admission.lock')
        fcntl.flock(turnstile, fcntl.LOCK_EX)
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
        os.close(turnstile)
        descriptors.remove(turnstile)
        for fd in descriptors:
            os.set_inheritable(fd, True)
        return descriptors
    except BaseException:
        for fd in descriptors:
            os.close(fd)
        raise


def acquire_weighted(state_dir, *, integration=False, cwd=None):
    """Three 3-GiB reservations: an API uses one; a build/browser uses two.

    Enable only after legacy exclusive callers have drained or been requeued,
    reserving a token for each legacy API still running. All heavy callers retain
    the shared legacy gate, so an old exclusive caller still waits safely.
    """
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    descriptors = []
    def lock_file(name):
        fd = os.open(state / name, os.O_CREAT | os.O_RDWR, 0o600)
        descriptors.append(fd)
        return fd
    try:
        if cwd is not None:
            name = hashlib.sha256(os.fsencode(Path(cwd).resolve())).hexdigest()
            fcntl.flock(lock_file('worktree-' + name + '.lock'), fcntl.LOCK_EX)
        if not integration:
            fcntl.flock(lock_file('browser-build.lock'), fcntl.LOCK_EX)
        admission = lock_file('weighted-admission.lock')
        fcntl.flock(admission, fcntl.LOCK_EX)
        fcntl.flock(lock_file('heavy-validation.lock'), fcntl.LOCK_SH)
        tokens = [lock_file(f'validation-budget-{i}.lock') for i in range(3)]
        required = 1 if integration else 2
        selected = []
        while len(selected) < required:
            for fd in tokens:
                if fd in selected:
                    continue
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                selected.append(fd)
                if len(selected) == required:
                    break
            if len(selected) < required:
                time.sleep(.1)
        for fd in tokens:
            if fd not in selected:
                os.close(fd)
                descriptors.remove(fd)
        os.close(admission)
        descriptors.remove(admission)
        for fd in descriptors:
            os.set_inheritable(fd, True)
        return descriptors
    except BaseException:
        for fd in descriptors:
            os.close(fd)
        raise
