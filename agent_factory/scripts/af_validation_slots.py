"""Admission for bounded isolated API checks, or one exclusive browser/build check.

The original global lock remains the gate, so older exclusive callers stay safe.
Returned descriptors must survive exec and remain open for the command's lifetime.
"""
import fcntl
import hashlib
import os
from pathlib import Path
import time


def worktree_lock_paths(cwd):
    """One checkout lock across subdirectories, retaining the older cwd lock."""
    current = Path(cwd).resolve()
    root = next((p for p in (current, *current.parents) if (p / '.git').exists()), current)
    return [root] if root == current else [root, current]


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
            for path in worktree_lock_paths(cwd):
                name = hashlib.sha256(os.fsencode(path)).hexdigest()
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


def acquire_weighted(state_dir, *, integration=False, lightweight=False, cwd=None):
    """Three 3-GiB reservations: API/unit checks use one; a build/browser uses two.

    Enable only after legacy exclusive callers have drained or been requeued,
    reserving a token for each legacy API still running. All heavy callers retain
    the shared legacy gate, so an old exclusive caller still waits safely.
    """
    small = integration or lightweight
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    descriptors = []
    def lock_file(name):
        fd = os.open(state / name, os.O_CREAT | os.O_RDWR, 0o600)
        descriptors.append(fd)
        return fd
    try:
        if cwd is not None:
            for path in worktree_lock_paths(cwd):
                name = hashlib.sha256(os.fsencode(path)).hexdigest()
                fcntl.flock(lock_file('worktree-' + name + '.lock'), fcntl.LOCK_EX)
        if not small:
            fcntl.flock(lock_file('browser-build.lock'), fcntl.LOCK_EX)
        admission = lock_file('weighted-admission.lock')
        fcntl.flock(admission, fcntl.LOCK_EX)
        fcntl.flock(lock_file('heavy-validation.lock'), fcntl.LOCK_SH)
        tokens = [lock_file(f'validation-budget-{i}.lock') for i in range(3)]
        required = 1 if small else 2
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


def working_set_bytes(cgroup):
    """Exclude inactive file cache from admission pressure, retaining hard caps.

    memory.current includes clean cache left by completed builds. Treating all of
    it as active can strand a queue forever although MemAvailable is abundant.
    Missing/invalid accounting stays conservative by using the full current value.
    """
    root = Path(cgroup)
    current = int((root / 'memory.current').read_text())
    try:
        stats = dict(line.split() for line in (root / 'memory.stat').read_text().splitlines())
        inactive = max(int(stats.get('inactive_file', '0')), 0)
    except (OSError, ValueError):
        inactive = 0
    return max(current - inactive, 0)


def validation_kind(command, args, *, unit_scripts=()):
    """Classify direct test tools, plus npm scripts audited by this project.

    Unknown scripts retain the conservative build/browser allocation. A script
    named 'test' is not assumed lightweight unless the caller audited its body.
    """
    name = Path(command).name
    if name.startswith('test-harness') and 'test:integration' in args:
        return 'api'
    if name.startswith(('vitest', 'tsc', '_tsc', 'eslint', 'prettier')):
        return 'unit'
    if name in {'npm', 'npm-cli.js', 'npx', 'npx-cli.js'}:
        script = None
        if 'run' in args:
            i = args.index('run')
            script = args[i+1] if len(args) > i+1 else None
        elif args and args[0] in {'test', 't'}:
            script = 'test'
        if script in unit_scripts:
            return 'unit'
    return 'build'
