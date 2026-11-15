"""Diagnostic timing observations; never a dispatch or validation state store."""
from datetime import datetime
import json
from pathlib import Path
import time


def measure(facts: list[dict], events: list[dict], now: float) -> dict:
    starts = {}
    for event in events:
        if event.get('event') == 'worker_launch' and event.get('ticket'):
            ticket = event['ticket']
            starts[ticket] = min(starts.get(ticket, float('inf')), event['at'])
    counts, tickets = {}, []
    for fact in facts:
        meta = fact.get('meta', {})
        state = meta.get('build_state', 'incomplete')
        counts[state] = counts.get(state, 0) + 1
        ticket = meta.get('requirement_id')
        if ticket not in starts:
            continue
        end = now
        if state == 'finished' and meta.get('finished_at'):
            end = datetime.fromisoformat(meta['finished_at'].replace('Z', '+00:00')).timestamp()
        minutes = round((end - starts[ticket]) / 60, 2)
        tickets.append(dict(ticket=ticket, state=state, minutes=minutes,
                            above_target=minutes >= 20, start=starts[ticket],
                            finished_at=meta.get('finished_at')))
    return dict(at=now, total=len(facts), counts=counts, tickets=tickets)


def observe(facts: list[dict], config: dict, *, force: bool = False) -> dict | None:
    state = Path(config['runtime']) / 'state'
    stamp = state / 'last-timing-observation'
    now = time.time()
    if not force and stamp.exists() and now - stamp.stat().st_mtime < 1200:
        return None
    events = []
    path = state / 'worker-start-events.jsonl'
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue  # An in-flight append is not evidence a worker stopped.
    report = measure(facts, events, now)
    with (state / 'timing-observations.jsonl').open('a') as stream:
        stream.write(json.dumps(report) + '\n')
    stamp.touch(mode=0o600)
    return report
