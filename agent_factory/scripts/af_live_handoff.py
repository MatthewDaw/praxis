"""One-shot scheduler handoff that preserves explicitly named live workers.

An operator writes a short-lived, private handoff after inspecting worker sessions.
Cleanup may skip once; the next driver consumes it once. Any failed retry returns
immediately to normal scope cleanup. This never changes ticket/check outcomes.
"""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time


def handoff_path():
    state = os.environ.get('AF_STATE_DIR')
    root = Path(state) if state else Path(__file__).resolve().parents[2] / 'state'
    project = os.environ.get('FACTORY_PROJECT', '')
    return root / f'af-live-handoff-{project}.json'


def read_valid(path):
    try:
        stat = path.stat()
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not data.get('owner_unit') or not data.get('project'):
        return None
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        return None
    if data.get('project') != os.environ.get('FACTORY_PROJECT'):
        return None
    if data.get('owner_unit') != os.environ.get('AF_SERVICE_UNIT'):
        return None
    expires = data.get('expires_at')
    if not isinstance(expires, (int, float)) or not time.time() < expires <= time.time() + 600:
        return None
    ids, sessions = data.get('ids'), data.get('sessions')
    if not isinstance(ids, list) or not ids or not isinstance(sessions, list) or len(ids) != len(sessions):
        return None
    next_ids, next_sessions = data.get('next_ids', []), data.get('next_sessions', [])
    if not isinstance(next_ids, list) or not isinstance(next_sessions, list) or len(next_ids) != len(next_sessions):
        return None
    if any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', v)
           for v in ids + sessions + next_ids + next_sessions):
        return None
    if len(set(ids + next_ids)) != len(ids + next_ids) or len(set(sessions + next_sessions)) != len(sessions + next_sessions):
        return None
    if subprocess.run(['systemctl', '--user', 'is-active', '--quiet', data['owner_unit']], capture_output=True).returncode:
        return None
    if any(subprocess.run(['tmux', 'has-session', '-t', session], capture_output=True).returncode for session in sessions + next_sessions):
        return None
    return data


def skip_cleanup_once(path=None):
    path = Path(path) if path is not None else handoff_path()
    data = read_valid(path)
    if data is None or data.get('cleanup_skipped'):
        return False
    data['cleanup_skipped'] = True
    temp = path.with_suffix('.next')
    temp.write_text(json.dumps(data))
    temp.chmod(0o600)
    temp.replace(path)
    return True


def consume(path=None):
    path = Path(path) if path is not None else handoff_path()
    data = read_valid(path)
    if data is None or data.get('cleanup_skipped') is not True:
        return ''
    path.unlink()
    values = {'AF_CARRY_IDS': ' '.join(data['ids']),
              'AF_CARRY_SESSIONS': ' '.join(data['sessions']), 'AF_ROLLING': '1',
              'AF_HANDOFF_NEXT_IDS': ' '.join(data.get('next_ids', [])),
              'AF_HANDOFF_NEXT_SESSIONS': ' '.join(data.get('next_sessions', []))}
    return '\n'.join(f'export {key}={shlex.quote(value)}' for key, value in values.items())


if __name__ == '__main__':
    if sys.argv[1:] == ['consume']:
        print(consume())
    else:
        raise SystemExit('usage: af_live_handoff.py consume')
