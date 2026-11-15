import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).parents[1] / 'scripts'
SOURCE = (SCRIPTS / 'af-ticket-loop.sh').read_text()


def launch_functions():
    a = SOURCE.index('af_launch_agent(){')
    b = SOURCE.index('\naf_wait_ready(){', a)
    definitions = '\n'.join(line for line in SOURCE.splitlines()
                            if line.startswith(('af_native_fanout(){', 'af_prompt_is_argv(){')))
    return definitions + '\n' + SOURCE[a:b]


@pytest.mark.parametrize('backend,expected', [('sonnet', 0), ('codex', 0), ('deepseek', 1)])
def test_backend_native_fanout_is_real_shell_behavior(backend, expected):
    result = subprocess.run(['bash', '-c', launch_functions() + '\naf_native_fanout'],
                            env={**os.environ, 'BACKEND': backend}, capture_output=True, text=True)
    assert result.returncode == expected


def test_three_sonnet_workers_get_private_prompts_and_independent_native_launches(tmp_path):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    for name in ['af_launch_prompt.py', 'af_resume_context.py']:
        shutil.copyfile(SCRIPTS / name, scripts / name)
    state = tmp_path / 'state'
    state.mkdir()
    log = tmp_path / 'tmux.log'
    harness = scripts / 'driver.sh'
    harness.write_text('set -eu\n' + launch_functions() + '''
say(){ :; }
tmux(){ printf '%s\\n' "$*" >> "$TMUX_LOG"; }
for ticket in R1 R2 R3; do
  af_launch_agent "$SESSION-$ticket" "/af-build shop $ticket — build exactly this ONE ticket"
done
''')
    tree = tmp_path / 'worktree with spaces'
    tree.mkdir()
    env = {**os.environ, 'BACKEND': 'sonnet', 'CLAUDE_LAUNCH': 'claude --model sonnet',
           'AF_PLUGIN_DIR': str(tmp_path / 'factory with spaces'), 'AF_STATE_DIR': str(state),
           'AF_OWNER_EXECUTION_POLICY': 'Payment scope only.', 'PY': sys.executable,
           'PROJECT': 'shop', 'FACTORY_PROJECT': 'shop', 'SESSION': 'af-shop',
           'WT': str(tree), 'TMUX_LOG': str(log)}
    subprocess.run(['bash', str(harness)], env=env, check=True, capture_output=True)
    launches = [line for line in log.read_text().splitlines() if line.startswith('send-keys')]
    assert len(launches) == 3
    assert all('--plugin-dir' in line and 'worktree\\ with\\ spaces' in line for line in launches)
    for ticket in ['R1', 'R2', 'R3']:
        prompt = state / ('af-af-shop-' + ticket + '.prompt')
        assert prompt.stat().st_mode & 0o077 == 0
        text = prompt.read_text()
        assert text.startswith('Execute this factory assignment.')
        assert 'Payment scope only.' in text and '/af-build shop ' + ticket in text


def test_failed_background_verification_stays_failed_and_checkpoint_survives(tmp_path):
    driver = tmp_path / 'test.sh'
    driver.write_text('''set -u
source "$RECOVERY"
say(){ :; }
( exit 9 ) & AF_VERIFY_PID=$!
af_wait_verify
result=$?
printf 'status=%s\\n' "$result"
test "$result" = 9
''')
    result = subprocess.run(['bash', str(driver)], env={**os.environ, 'RECOVERY': str(SCRIPTS / 'af-recovery.sh')},
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert 'status=9' in result.stdout


def test_sonnet_resume_validates_project_and_original_transcript(tmp_path):
    sys.path.insert(0, str(SCRIPTS))
    from af_resume_context import resume_session
    state = tmp_path / 'state'
    (state / 'retired-workers').mkdir(parents=True)
    session = '00000000-0000-4000-8000-000000000001'
    rollout = tmp_path / (session + '.jsonl')
    rollout.write_text(json.dumps({'type': 'user', 'sessionId': session}) + '\n')
    receipt = state / 'retired-workers/R1-claude.json'
    receipt.write_text(json.dumps(dict(project='shop', ticket='R1', tmux_session='af-store-R1',
                         terminal_state='finished', claude_session_id=session, rollout=str(rollout))))
    receipt.chmod(0o600)
    assert resume_session(state, 'shop', 'R1', 'sonnet') == session
    assert resume_session(state, 'another-project', 'R1', 'sonnet') == ''
    assert resume_session(state, 'shop', 'R1', 'codex') == ''


def test_retry_handoff_is_prepared_before_worktree_cleanup():
    # Calls are ordered in the actual EXIT handler, not in a second recovery path.
    body = SOURCE[SOURCE.index('af_cleanup_on_exit(){'):SOURCE.index('AF_ROUNDS_UNVERIFIED=0')]
    assert body.index('--prepare-handoff') < body.index('af_live_handoff.py') < body.index('sweep_worktrees')
