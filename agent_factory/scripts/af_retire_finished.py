#!/usr/bin/env python3
"""Close finished, idle Codex CLIs while retaining their resumable sessions."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


def retire_finished(facts: list[dict[str, Any]], project: str, state: Path, *,
                    proc: Path = Path('/proc'), run: Any = subprocess.run,
                    now: datetime | None = None) -> list[str]:
    retired = []
    current = now or datetime.now(timezone.utc)
    for fact in facts:
        meta = fact.get('meta') or {}
        if meta.get('build_state') != 'finished' or meta.get('claim_owner'):
            continue
        try:
            finished = datetime.fromisoformat(meta['finished_at'].replace('Z', '+00:00'))
            if (current - finished).total_seconds() < 30:
                continue
            ticket = meta['requirement_id']
            if not isinstance(ticket, str) or Path(ticket).name != ticket or ticket in {'.', '..'}:
                continue
            session = f'af-{project}-{ticket}'
            pane = run(['tmux', 'capture-pane', '-p', '-t', session],
                       capture_output=True, text=True, check=True).stdout
            footer = pane[-650:]
            if ('Ask Codex to do anything' not in footer or 'Worked for ' not in pane
                    or any(s in footer for s in ('Working (', 'Waiting for background', 'Compacting context'))):
                continue
            roots = run(['tmux', 'list-panes', '-t', session, '-F', '#{pane_pid}'],
                        capture_output=True, text=True, check=True).stdout.split()
            if len(roots) != 1:
                continue
            pending = list(roots)
            contexts = []
            while pending:
                pid = pending.pop()
                root = proc / pid
                children = root / 'task' / pid / 'children'
                if children.exists():
                    pending.extend(children.read_text().split())
                if (root / 'exe').resolve().name != 'codex':
                    continue
                for fd in (root / 'fd').iterdir():
                    target = fd.readlink()
                    if target.name.startswith('rollout-') and target.suffix == '.jsonl' and target.is_file():
                        with target.open() as stream:
                            context = json.loads(stream.readline())['payload']['id']
                        if not isinstance(context, str) or not context:
                            continue
                        contexts.append({'pid': int(pid), 'codex_session_id': context,
                                         'rollout': str(target)})
            if len(contexts) != 1:
                continue
            archive = state / 'retired-workers'
            archive.mkdir(parents=True, exist_ok=True)
            archive.chmod(0o700)
            record = {'ticket': ticket, 'uuid': fact['id'], 'tmux_session': session,
                      'finished_at': meta['finished_at'], 'retired_at': current.isoformat(),
                      **contexts[0]}
            for name, value in [(ticket + '.json', json.dumps(record, indent=2)),
                                (ticket + '.pane.txt', pane)]:
                target = archive / name
                temporary = target.with_suffix(target.suffix + '.new')
                temporary.write_text(value)
                temporary.chmod(0o600)
                temporary.replace(target)
            # Keep the terminal and its output available to handoff/diagnostics.
            if run(['tmux', 'capture-pane', '-p', '-t', session],
                   capture_output=True, text=True, check=True).stdout != pane:
                continue
            run(['tmux', 'set-option', '-w', '-t', session, 'remain-on-exit', 'on'], check=True)
            run(['tmux', 'send-keys', '-t', session, 'C-d'], check=True)
            retired.append(ticket)
        except (KeyError, ValueError, OSError, subprocess.SubprocessError):
            # A missing pane or unproven idle context never justifies termination.
            continue
    return retired


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hooks'))
    import _praxis as praxis
    facts = praxis.facts_by(category='requirement', space=args.project,
                           snapshot='prd-' + args.project)
    for ticket in retire_finished(facts, args.project, args.state_dir):
        print(f'Retired finished idle {ticket}; Codex session and transcript retained')


if __name__ == '__main__':
    main()
