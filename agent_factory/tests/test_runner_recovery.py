"""Failure injection against the actual shipped shell helpers and supervisor."""
from pathlib import Path
import os
import shlex
import subprocess

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def shell(body, cwd, *, check=True):
    return subprocess.run(['bash', '-c', 'set -eu\nsource ' + shlex.quote(str(SCRIPTS / 'af-recovery.sh')) + '\n' + body], cwd=cwd, capture_output=True, text=True, check=check)


def setup_repo(tmp):
    subprocess.run(['git', 'init', '-q', str(tmp)], check=True)
    subprocess.run(['git', '-C', str(tmp), '-c', 'user.name=test', '-c', 'user.email=test@test', 'commit', '--allow-empty', '-qm', 'base'], check=True)
    return f'WT={shlex.quote(str(tmp))}; AF_STATE_DIR=$WT; PROJECT=demo; AF_PREMERGE_SHA=\nsay(){{ echo "$*"; }}\n'


def test_failed_verification_survives_process_restart(tmp_path):
    env = setup_repo(tmp_path)
    shell(env + 'af_checkpoint_verification "$(git rev-parse HEAD)" 7 R001 R002', tmp_path)
    pending = tmp_path / 'af-pending-verification-demo'
    assert pending.exists()
    failed = shell(env + 'verify_round(){ return 9; }; af_resume_verification; echo UNSAFE_DISPATCH', tmp_path, check=False)
    assert failed.returncode == 9
    assert 'UNSAFE_DISPATCH' not in failed.stdout
    assert pending.exists()
    success = shell(env + 'verify_round(){ echo "verified:$*:$AF_PREMERGE_SHA"; af_clear_verification_checkpoint; }; af_resume_verification; echo DISPATCH', tmp_path)
    assert 'verified:7 R001 R002:' in success.stdout
    assert success.stdout.rstrip().endswith('DISPATCH')
    assert not pending.exists()


def test_changed_integration_tree_cannot_clear_old_checkpoint(tmp_path):
    env = setup_repo(tmp_path)
    shell(env + 'af_checkpoint_verification "$(git rev-parse HEAD)" 1 R001', tmp_path)
    subprocess.run(['git', '-C', str(tmp_path), '-c', 'user.name=test', '-c', 'user.email=test@test', 'commit', '--allow-empty', '-qm', 'changed'], check=True)
    result = shell(env + 'verify_round(){ echo UNSAFE_VERIFICATION; }; af_resume_verification', tmp_path, check=False)
    assert result.returncode == 9
    assert 'UNSAFE_VERIFICATION' not in result.stdout
    assert (tmp_path / 'af-pending-verification-demo').exists()


def test_activity_extends_verification_and_idle_turn_detection_is_conservative(tmp_path):
    for idle, busy, expected in [(0, 0, 0), (30, 1, 0), (30, 0, 1)]:
        assert shell(f'af_verification_can_extend {idle} 30 {busy}', tmp_path, check=False).returncode == expected
    for pane, expected in [('  Worked for 42s\n› Ask Codex to do anything', 0), ('• Working (2m • esc to interrupt)', 1), ('some arbitrary tool output', 1), ('Worked for 2s\n• Working (esc to interrupt)', 1)]:
        assert shell('af_worker_turn_ended ' + shlex.quote(pane), tmp_path, check=False).returncode == expected


def test_supervisor_retries_failure_and_stops_on_success(tmp_path):
    child = tmp_path / 'child.sh'
    child.write_text('n=0; [ ! -f count ] || n=$(cat count)\nn=$((n+1)); echo "$n" > count\n[ "$n" -gt 1 ]\n')
    result = subprocess.run(['bash', str(SCRIPTS / 'af-supervise.sh'), 'bash', str(child)], cwd=tmp_path, env={**os.environ, 'AF_SUPERVISOR_RETRY_S': '0'}, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    assert (tmp_path / 'count').read_text().strip() == '2'
    assert 'driver exited 1' in result.stderr


def test_supervisor_does_not_retry_operator_stop(tmp_path):
    child = tmp_path / 'child.sh'
    child.write_text('echo started >> count\nexec sleep 30\n')
    proc = subprocess.Popen(['bash', str(SCRIPTS / 'af-supervise.sh'), 'bash', str(child)], cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    import time
    for _ in range(100):
        if (tmp_path / 'count').exists():
            break
        time.sleep(.01)
    proc.terminate()
    proc.communicate(timeout=5)
    assert proc.returncode == 130
    assert (tmp_path / 'count').read_text().splitlines() == ['started']


def test_one_idle_worker_does_not_end_another_workers_live_turn(tmp_path):
    ended = 'Worked for 2s\n› Ask Codex to do anything'
    active = '• Working (3s • esc to interrupt)'
    base = 'AF_WORKER_SESSIONS="one two"; SESSION=unused\nverify_children_busy(){ return 1; }\n'
    mock = 'tmux(){ [ "$1" != has-session ] || return 0; if [ "$3" = one ]; then printf "%s" "$IDLE"; else printf "%s" "$OTHER"; fi; }\n'
    for other, expected in [(active, 1), (ended, 0)]:
        result = shell(base + 'IDLE=' + shlex.quote(ended) + '; OTHER=' + shlex.quote(other) + '\n' + mock + 'af_all_worker_turns_ended', tmp_path, check=False)
        assert result.returncode == expected


def test_background_verification_failure_cannot_be_swallowed(tmp_path):
    result = shell('say(){ :; }; (exit 9) & AF_VERIFY_PID=$!; af_wait_verify; echo UNSAFE_INTEGRATION', tmp_path, check=False)
    assert result.returncode == 9
    assert 'UNSAFE_INTEGRATION' not in result.stdout
