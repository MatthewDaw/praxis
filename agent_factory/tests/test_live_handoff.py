import importlib.util
import json
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'af_live_handoff.py'
spec = importlib.util.spec_from_file_location('handoff', SCRIPT)
handoff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handoff)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv('FACTORY_PROJECT', 'example')
    monkeypatch.setenv('AF_SERVICE_UNIT', 'example.service')
    monkeypatch.setattr(handoff.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0))
    p = tmp_path / 'handoff.json'
    data = {'project': 'example', 'owner_unit': 'example.service',
            'expires_at': time.time() + 240, 'ids': ['R001', 'R003'],
            'sessions': ['af-example-R001', 'af-example-R003']}
    p.write_text(json.dumps(data))
    p.chmod(0o600)
    return p


def test_cleanup_skips_only_once_then_driver_consumes_once(state):
    assert handoff.consume(state) == ''
    assert handoff.skip_cleanup_once(state)
    assert not handoff.skip_cleanup_once(state)
    exports = handoff.consume(state)
    assert "export AF_CARRY_IDS='R001 R003'" in exports
    assert 'export AF_ROLLING=1' in exports
    assert not state.exists()
    assert handoff.consume(state) == ''
    assert not handoff.skip_cleanup_once(state)


@pytest.mark.parametrize('mutation', [
    {'project': 'other'}, {'owner_unit': 'other.service'},
    {'expires_at': 0}, {'expires_at': time.time() + 3600},
    {'ids': ['R001; exit 0']}, {'sessions': []}, {'owner_unit': ''},
])
def test_invalid_handoff_never_suppresses_cleanup(state, mutation):
    data = json.loads(state.read_text()); data.update(mutation)
    state.write_text(json.dumps(data))
    assert not handoff.skip_cleanup_once(state)
    assert handoff.consume(state) == ''


def test_missing_live_session_never_suppresses_cleanup(state, monkeypatch):
    monkeypatch.setattr(handoff.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1))
    assert not handoff.skip_cleanup_once(state)


def test_handoff_must_be_private_and_a_json_object(state):
    state.chmod(0o644)
    assert not handoff.skip_cleanup_once(state)
    state.chmod(0o600)
    state.write_text('[]')
    assert not handoff.skip_cleanup_once(state)


@pytest.mark.parametrize("first_exit", [0, 9])
def test_real_supervisor_retries_driver_and_adopts_same_sessions(tmp_path, first_exit):
    import os
    scripts = SCRIPT.parent
    state_dir = tmp_path / 'state'; state_dir.mkdir()
    bins = tmp_path / 'bin'; bins.mkdir()
    for name in ['systemctl', 'tmux']:
        p = bins / name; p.write_text('#!/bin/sh\nexit 0\n'); p.chmod(0o755)
    path = state_dir / 'af-live-handoff-example.json'
    path.write_text(json.dumps({'project': 'example', 'owner_unit': 'example.service',
        'expires_at': time.time() + 240, 'ids': ['R001', 'R003'],
        'sessions': ['af-example-R001', 'af-example-R003']})); path.chmod(0o600)
    driver = tmp_path / 'driver.sh'
    driver.write_text('''#!/bin/bash
if [ ! -e "$AF_STATE_DIR/attempt" ]; then
  touch "$AF_STATE_DIR/attempt"
  exit FIRST_EXIT
fi
AF_CARRY_IDS=""; AF_CARRY_SESSIONS=""; AF_VERIFY_PID=""
exports=$("$HANDOFF_PYTHON" "$HANDOFF_SCRIPT" consume) || exit 125
eval "$exports"
printf '%s|%s|%s' "$AF_CARRY_IDS" "$AF_CARRY_SESSIONS" "$AF_ROLLING" > "$AF_STATE_DIR/adopted"
'''.replace('FIRST_EXIT',str(first_exit)))
    env = {**os.environ, 'PATH': str(bins) + os.pathsep + os.environ['PATH'],
        'AF_STATE_DIR': str(state_dir), 'FACTORY_PROJECT': 'example',
        'AF_SERVICE_UNIT': 'example.service', 'AF_SUPERVISOR_RETRY_S': '300',
        'HANDOFF_SCRIPT': str(SCRIPT), 'HANDOFF_PYTHON': __import__('sys').executable}
    run = subprocess.run(['bash', str(scripts/'af-supervise.sh'), 'bash', str(driver)],
                         env=env, capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    assert (state_dir/'adopted').read_text() == 'R001 R003|af-example-R001 af-example-R003|1'
    assert not path.exists()
    assert 'preserving live worker scopes' in run.stderr


def test_driver_applies_handoff_after_project_settings_exports():
    driver = (SCRIPT.parent/'af-ticket-loop.sh').read_text()
    settings = driver.index("d = json.load(open('$WT/.claude/settings.local.json'))['env']")
    assert driver.index('af_live_handoff.py" consume') > settings


def test_handoff_keeps_current_and_next_batch_separate(state):
    data = json.loads(state.read_text())
    data.update(next_ids=['R010'], next_sessions=['af-example-R010'])
    state.write_text(json.dumps(data))
    assert handoff.skip_cleanup_once(state)
    exports = handoff.consume(state)
    assert "export AF_CARRY_IDS='R001 R003'" in exports
    assert 'export AF_HANDOFF_NEXT_IDS=R010' in exports
    assert 'export AF_HANDOFF_NEXT_SESSIONS=af-example-R010' in exports
    assert not state.exists()


@pytest.mark.parametrize('mutation', [
    {'next_ids':['R010'], 'next_sessions':[]},
    {'next_ids':['R001'], 'next_sessions':['af-example-R010']},
    {'next_ids':['R010'], 'next_sessions':['af-example-R001']},
    {'next_ids':['R010; exit'], 'next_sessions':['af-example-R010']},
])
def test_invalid_next_batch_never_suppresses_cleanup(state, mutation):
    data=json.loads(state.read_text());data.update(mutation);state.write_text(json.dumps(data))
    assert not handoff.skip_cleanup_once(state)


def test_next_batch_lease_is_preserved_and_restored_after_current_adoption():
    driver=(SCRIPT.parent/'af-ticket-loop.sh').read_text()
    assert "carry=set(os.environ.get('AF_CARRY_IDS','').split()) | set(os.environ.get('AF_HANDOFF_NEXT_IDS','').split())" in driver
    assert 'AF_CARRY_IDS="${AF_HANDOFF_NEXT_IDS:-}"; AF_CARRY_SESSIONS="${AF_HANDOFF_NEXT_SESSIONS:-}"' in driver
