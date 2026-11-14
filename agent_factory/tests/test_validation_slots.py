from pathlib import Path
import select
import subprocess
import sys

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def start(state, *, integration, cwd=None):
    code = 'import sys; sys.path.insert(0,sys.argv[1]); from af_validation_slots import acquire; fds=acquire(sys.argv[2],integration=sys.argv[3]=="1",cwd=sys.argv[4] or None); print("admitted",flush=True); sys.stdin.read(1)'
    return subprocess.Popen([sys.executable, '-c', code, str(SCRIPTS), str(state), '1' if integration else '0', str(cwd or '')], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def ready(proc, timeout=2):
    available, _, _ = select.select([proc.stdout], [], [], timeout)
    if not available:
        return False
    assert proc.stdout.readline().strip() == 'admitted'
    return True


def release(proc):
    proc.stdin.write('\n')
    proc.stdin.flush()
    proc.communicate(timeout=3)
    assert proc.returncode == 0


def cleanup(*processes):
    for proc in processes:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=3)


def test_two_integrations_overlap_third_waits_and_exclusive_waits_for_both(tmp_path):
    a = start(tmp_path, integration=True)
    b = start(tmp_path, integration=True)
    assert ready(a) and ready(b)
    c = start(tmp_path, integration=True)
    try:
        assert not ready(c, .15)
        release(a)
        assert ready(c)
        exclusive = start(tmp_path, integration=False)
        try:
            assert not ready(exclusive, .15)
            release(b)
            assert not ready(exclusive, .15)
            release(c)
            assert ready(exclusive)
            release(exclusive)
        finally:
            cleanup(exclusive)
    finally:
        cleanup(a, b, c)


def test_same_worktree_serializes_even_with_two_available_slots(tmp_path):
    a = start(tmp_path, integration=True, cwd=tmp_path/'worktree')
    assert ready(a)
    b = start(tmp_path, integration=True, cwd=tmp_path/'worktree')
    try:
        assert not ready(b, .15)
        release(a)
        assert ready(b)
        release(b)
    finally:
        cleanup(a, b)
