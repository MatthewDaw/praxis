"""Continue a stopped ticket conversation without restarting its process or work."""
from pathlib import Path
import argparse
import json
import re
import subprocess
import time


def needs_continuation(state: str, command: str, pane: str) -> bool:
    if state not in {'incomplete', 'in_progress'} or command not in {'claude', 'codex'}:
        return False
    lower = pane.lower()
    if any(s in lower for s in ['esc to interrupt', 'waiting for background', 'queued message',
                                'queued follow-up', 'ctrl+x ctrl+s to send now']):
        return False
    return ('bypass permissions on' in lower if command == 'claude'
            else 'ask codex to do anything' in lower)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--session-prefix', required=True)
    args = parser.parse_args()
    import _praxis as p
    records = args.state_dir / 'idle-worker-continuations'
    records.mkdir(exist_ok=True, mode=0o700)
    for fact in p.facts_by(category='requirement', space=args.project, snapshot='prd-' + args.project):
        meta = fact.get('meta', {})
        ticket = str(meta.get('requirement_id', ''))
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', ticket):
            continue
        session = args.session_prefix + '-' + ticket
        command = subprocess.run(['tmux', 'display-message', '-p', '-t', session,
                                  '#{pane_current_command}'], capture_output=True, text=True).stdout.strip()
        if command not in {'claude', 'codex'}:
            continue
        pane = subprocess.run(['tmux', 'capture-pane', '-p', '-t', session],
                              capture_output=True, text=True).stdout
        if not needs_continuation(meta.get('build_state', ''), command, pane):
            continue
        receipt = records / (ticket + '.json')
        now = time.time()
        if receipt.exists() and now - receipt.stat().st_mtime < 120:
            continue
        # A second capture avoids sending into a conversation that just resumed.
        current = subprocess.run(['tmux', 'capture-pane', '-p', '-t', session],
                                 capture_output=True, text=True).stdout
        if current != pane:
            continue
        message = (
            'Continue this existing ticket now; authoritative Praxis state is still unfinished. '
            'Preserve your branch, context and completed evidence. Claim or heartbeat the existing '
            'ticket without resetting checks. Complete only the remaining changed-code and affected '
            'league checks, record real results, and release finished when covered. Do not stop with '
            'a to-do list or wait for approval. Do not restart implementation or run unrelated legacy '
            'video suites. Record a blocker only for genuine technical inability.'
        )
        subprocess.run(['tmux', 'send-keys', '-t', session, '-l', message], check=True)
        subprocess.run(['tmux', 'send-keys', '-t', session, 'Enter'], check=True)
        receipt.write_text(json.dumps({'ticket': ticket, 'at': now, 'session': session}))
        receipt.chmod(0o600)
        print(ticket, 'continued existing idle conversation')


if __name__ == '__main__':
    main()
