"""Deliver a private operator note only during a live worker's own heartbeat.

Unlike a queued UI prompt this cannot start another turn after ticket completion.
Notes are advisory text, never executable configuration or validation outcomes.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import time


def deliver_once(cid: str, owner: str) -> bool:
    state = os.environ.get('AF_STATE_DIR')
    project = os.environ.get('FACTORY_PROJECT')
    if not state or not project:
        return False
    key = hashlib.sha256(f'{project}:{cid}'.encode()).hexdigest()
    path = Path(state) / 'operator-notices' / (key + '.json')
    try:
        stat = path.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            return False
        note = json.loads(path.read_text())
        if not isinstance(note, dict) or note.get('owner') != owner:
            return False
        if note.get('project') != project or note.get('cid') != cid:
            return False
        expires = note.get('expires_at')
        if not isinstance(expires, (float, int)) or expires < time.time():
            return False
        prefix = note.get('cwd_prefix')
        if not isinstance(prefix, str) or not prefix.startswith('/'):
            return False
        if not str(Path.cwd().resolve()).startswith(prefix):
            return False
        message = note.get('message')
        if not isinstance(message, str) or not message.strip() or len(message) > 12000:
            return False
        path.replace(path.with_suffix('.delivered'))
    except (OSError, ValueError):
        return False
    print(f'[operator workflow note for active ticket {cid}]\n{message}', file=sys.stderr, flush=True)
    return True
