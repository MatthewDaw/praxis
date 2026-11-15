"""Release terminal idle Claude scopes after archiving their original conversation."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess


def idle(pane: str) -> bool:
    footer = pane[-1000:].lower()
    return ('bypass permissions on' in footer and 'esc to interrupt' not in footer
            and 'queued' not in footer and 'ctrl+x ctrl+s' not in footer
            and not re.search(r'\b[1-9]\d* (?:shells?|monitors?)\b', footer))


def context(config: Path, project: str, ticket: str) -> tuple[str, str] | None:
    pattern = re.compile(r'/af-build\s+' + re.escape(project) + r'\s+' + re.escape(ticket) + r'\b')
    for path in sorted((config / 'projects').glob('**/*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            with path.open() as stream:
                for index, line in enumerate(stream):
                    if index > 30:
                        break
                    event = json.loads(line)
                    if event.get('type') != 'user':
                        continue
                    content = event.get('message', {}).get('content', '')
                    text = content if isinstance(content, str) else ' '.join(
                        item.get('text', '') for item in content if item.get('type') == 'text')
                    if pattern.search(text) and event.get('sessionId') == path.stem:
                        return path.stem, str(path)
        except (OSError, ValueError, TypeError):
            continue
    return None


def retire(facts: list[dict], config: dict, passed, *, run=subprocess.run,
           proc: Path = Path('/proc')) -> list[str]:
    retired = []
    state = Path(config['runtime']) / 'state'
    for fact in facts:
        meta = fact.get('meta', {})
        ticket = str(meta.get('requirement_id', ''))
        terminal = meta.get('build_state')
        if (terminal not in {'finished', 'blocked'} or meta.get('claim_owner')
                or not re.fullmatch(r'[A-Za-z0-9_.-]+', ticket)):
            continue
        if terminal == 'finished' and not passed(fact):
            continue
        if terminal == 'blocked' and not meta.get('block_reason'):
            continue
        session = config['session_prefix'] + '-' + ticket
        try:
            command = run(['tmux', 'display-message', '-p', '-t', session, '#{pane_current_command}'],
                          capture_output=True, text=True, check=True).stdout.strip()
            if command != 'claude':
                continue
            pane = run(['tmux', 'capture-pane', '-p', '-t', session],
                       capture_output=True, text=True, check=True).stdout
            if not idle(pane):
                continue
            roots = run(['tmux', 'list-panes', '-t', session, '-F', '#{pane_pid}'],
                        capture_output=True, text=True, check=True).stdout.split()
            pending, candidates, busy = list(roots), [], False
            while pending:
                pid = pending.pop()
                root = proc / pid
                children = root / 'task' / pid / 'children'
                if children.exists():
                    pending.extend(children.read_text().split())
                comm = (root / 'comm').read_text().strip()
                args = (root / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
                if comm == 'claude':
                    candidates.append(root)
                elif comm not in {'bash', 'systemd-run'} and not (
                        comm in {'python', 'python3'} and '-m knowledge.mcp' in args):
                    busy = True
            if busy or len(candidates) != 1:
                continue
            cgroup = (candidates[0] / 'cgroup').read_text().strip().splitlines()[-1].split(':', 2)[-1]
            unit = cgroup.rsplit('/', 1)[-1]
            if not unit.endswith('.scope'):
                continue
            owners = run(['systemctl', '--user', 'show', unit, '--property=BindsTo', '--value'],
                         capture_output=True, text=True, check=True).stdout.split()
            if config['service'] not in owners:
                continue
            found = context(Path(config['claude_config_dir']), config['project'], ticket)
            if found is None:
                continue
            if run(['tmux', 'capture-pane', '-p', '-t', session],
                   capture_output=True, text=True, check=True).stdout != pane:
                continue
            archive = state / 'retired-workers'
            archive.mkdir(exist_ok=True, parents=True, mode=0o700)
            record = dict(project=config['project'], ticket=ticket, backend='claude',
                          terminal_state=terminal, tmux_session=session,
                          claude_session_id=found[0], rollout=found[1],
                          finished_at=meta.get('finished_at'),
                          retired_at=datetime.now(timezone.utc).isoformat())
            for name, data in [(ticket + '-claude.json', json.dumps(record)),
                               (ticket + '-claude-pane.txt', pane)]:
                target = archive / name
                temporary = target.with_suffix('.next')
                fd = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w') as stream:
                    stream.write(data)
                temporary.replace(target)
            run(['systemctl', '--user', 'stop', unit], check=True)
            retired.append(ticket)
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            continue
    return retired
