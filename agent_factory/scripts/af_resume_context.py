"""Recover an archived factory worker conversation without starting fresh."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import uuid


def resume_session(state: Path, project: str, ticket: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', ticket):
        return ''
    path = state / 'retired-workers' / (ticket + '.json')
    try:
        stat = path.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            return ''
        record = json.loads(path.read_text())
        if record.get('ticket') != ticket:
            return ''
        if record.get('project') is not None:
            if record['project'] != project or not str(record.get('tmux_session', '')).endswith('-' + ticket):
                return ''
        elif record.get('tmux_session') != f'af-{project}-{ticket}':
            return ''
        if record.get('terminal_state', 'finished') not in {'finished', 'blocked'}:
            return ''
        session = str(uuid.UUID(record['codex_session_id']))
        rollout = Path(record['rollout'])
        with rollout.open() as stream:
            meta = json.loads(stream.readline())
        return session if meta.get('payload', {}).get('id') == session else ''
    except (OSError, ValueError, KeyError, TypeError):
        return ''


def resume_arguments(args: list[str], state: Path, project: str) -> list[str]:
    """Only transform the driver's interactive one-ticket prompt, never exec/help."""
    if len(args) < 2 or args[-2] != '--':
        return args
    match = re.match(r'^/af-build ([A-Za-z0-9_.-]+) ([A-Za-z0-9_.-]+) — build exactly this ONE ticket', args[-1])
    if match is None or match[1] != project:
        return args
    prefix = args[:-2]
    i = 0
    while i < len(prefix):
        if prefix[i] in {'--dangerously-bypass-approvals-and-sandbox', '--no-alt-screen'}:
            i += 1
        elif prefix[i] in {'--model', '-m', '-c', '--config', '--profile', '-p', '--cd', '-C'} and i+1 < len(prefix):
            i += 2
        else:
            return args
    session = resume_session(state, project, match[2])
    return [*prefix, 'resume', session, '--', args[-1]] if session else args


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--ticket', required=True)
    options = parser.parse_args()
    print(resume_session(options.state_dir, options.project, options.ticket))
