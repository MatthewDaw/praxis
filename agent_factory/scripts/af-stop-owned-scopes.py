#!/usr/bin/env python3
"""Reap scopes explicitly bound to this failed driver's service before retrying."""
import os
import subprocess
import sys
import re
from pathlib import Path


def preserve_active_workers(owner):
    """Adopt live project workers on retries; explicit owner stops still reap them."""
    project = os.environ.get('FACTORY_PROJECT')
    state = os.environ.get('AF_STATE_DIR')
    if not project or not state:
        return False
    if subprocess.run(['systemctl', '--user', 'is-active', '--quiet', owner], capture_output=True).returncode:
        return False
    prefix = os.environ.get('AF_SESSION_PREFIX') or 'af-' + Path.cwd().name
    from af_live_handoff import prepare
    import _praxis as p
    try:
        facts = p.facts_by(category='requirement', space=project, snapshot='prd-' + project)
        states = {f.get('meta', {}).get('requirement_id'): f.get('meta', {}).get('build_state') for f in facts}
    except Exception:
        states = None  # A backend outage must not destroy an otherwise live conversation.
    result = subprocess.run(['tmux', 'list-sessions', '-F', '#{session_name}'], capture_output=True, text=True)
    ids, sessions = [], []
    for session in result.stdout.splitlines():
        if not session.startswith(prefix + '-'):
            continue
        ticket = session[len(prefix) + 1:]
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', ticket) or ticket in {'verify', 'resolve'}:
            continue
        if states is not None and states.get(ticket) not in {'incomplete', 'in_progress'}:
            continue
        prompt = Path(state) / ('af-' + session + '.prompt')
        if not prompt.exists() or prompt.stat().st_uid != os.getuid() or prompt.stat().st_mode & 0o077:
            continue
        command = subprocess.run(['tmux', 'display-message', '-p', '-t', session,
                                  '#{pane_current_command}'], capture_output=True, text=True).stdout.strip()
        if command not in {'claude', 'codex'}:
            continue
        ids.append(ticket)
        sessions.append(session)
    return prepare(ids, sessions) if ids else False


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from af_live_handoff import handoff_path, read_valid, skip_cleanup_once
    if read_valid(handoff_path()) is not None:
        skip_cleanup_once()
        print('[recovery] preserving live worker scopes while the validated scheduler handoff is pending', file=sys.stderr)
        return 0
    owner = os.environ.get('AF_SERVICE_UNIT')
    if not owner:
        return 0
    if preserve_active_workers(owner):
        skip_cleanup_once()
        print('[recovery] active unfinished workers preserved for immediate scheduler adoption', file=sys.stderr)
        return 0
    result = subprocess.run(['systemctl', '--user', 'list-units', '--type=scope',
                             '--plain', '--no-legend', '--no-pager'], capture_output=True, text=True, check=True)
    for line in result.stdout.splitlines():
        unit = line.split()[0]
        bound = subprocess.run(['systemctl', '--user', 'show', unit, '--property=BindsTo', '--value'],
                               capture_output=True, text=True, check=True).stdout.split()
        if owner in bound:
            subprocess.run(['systemctl', '--user', 'stop', unit], check=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
