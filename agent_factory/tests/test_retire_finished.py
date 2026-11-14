from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/af_retire_finished.py'
spec = importlib.util.spec_from_file_location('af_retire_finished', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
NOW = datetime(2026, 11, 14, 5, 30, tzinfo=timezone.utc)


def fixture(tmp_path):
    proc = tmp_path / 'proc'
    root = proc / '12'
    (root / 'task/12').mkdir(parents=True)
    (root / 'task/12/children').write_text('')
    (root / 'fd').mkdir()
    (root / 'exe').symlink_to(tmp_path / 'bin/codex')
    rollout = tmp_path / 'rollout-example.jsonl'
    rollout.write_text(json.dumps({'payload': {'id': 'resumable-session-id'}}) + '\n')
    (root / 'fd/3').symlink_to(rollout)
    fact = {'id': 'ticket-uuid', 'meta': {'requirement_id': 'R007', 'build_state': 'finished',
            'claim_owner': None, 'finished_at': '2026-11-14T05:00:00+00:00'}}
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args[1] == 'capture-pane':
            return SimpleNamespace(stdout='Finished\nWorked for 1h\n› Ask Codex to do anything\n')
        return SimpleNamespace(stdout='12\n')
    return proc, fact, calls, run


def test_finished_idle_cli_keeps_context_and_terminal(tmp_path):
    proc, fact, calls, run = fixture(tmp_path)
    assert module.retire_finished([fact], 'example', tmp_path/'state', proc=proc, run=run, now=NOW) == ['R007']
    archive = tmp_path/'state/retired-workers'
    record = json.loads((archive/'R007.json').read_text())
    assert record['codex_session_id'] == 'resumable-session-id'
    assert record['pid'] == 12
    assert (archive/'R007.json').stat().st_mode & 0o777 == 0o600
    assert calls[-2:] == [['tmux','set-option','-w','-t','af-example-R007','remain-on-exit','on'],
                         ['tmux','send-keys','-t','af-example-R007','C-d']]


def test_active_claim_recent_finish_and_working_pane_are_never_closed(tmp_path):
    proc, fact, calls, run = fixture(tmp_path)
    for change in [{'build_state': 'in_progress'}, {'claim_owner': 'live-owner'},
                   {'finished_at': NOW.isoformat()}]:
        changed = {**fact, 'meta': {**fact['meta'], **change}}
        assert module.retire_finished([changed], 'example', tmp_path/'state', proc=proc, run=run, now=NOW) == []
    def working(args, **kwargs):
        if args[1] == 'capture-pane':
            return SimpleNamespace(stdout='Worked for 1h\nWorking (1m)\n› Ask Codex to do anything')
        return run(args, **kwargs)
    assert module.retire_finished([fact], 'example', tmp_path/'state', proc=proc, run=working, now=NOW) == []
    assert not any(c[1] == 'send-keys' for c in calls)


def test_missing_or_ambiguous_rollout_leaves_worker_alone(tmp_path):
    proc, fact, calls, run = fixture(tmp_path)
    (proc/'12/fd/4').symlink_to((proc/'12/fd/3').readlink())
    assert module.retire_finished([fact], 'example', tmp_path/'state', proc=proc, run=run, now=NOW) == []
    (proc/'12/fd/4').unlink(); (proc/'12/fd/3').unlink()
    assert module.retire_finished([fact], 'example', tmp_path/'state', proc=proc, run=run, now=NOW) == []
    assert not any(c[1] == 'send-keys' for c in calls)


def test_worker_that_resumes_after_initial_idle_observation_is_not_closed(tmp_path):
    proc, fact, calls, run = fixture(tmp_path)
    observed = 0
    def changed(args, **kwargs):
        nonlocal observed
        if args[1] == 'capture-pane':
            observed += 1
            if observed > 1:
                return SimpleNamespace(stdout='Working (new turn)\n› Ask Codex to do anything')
        return run(args, **kwargs)
    assert module.retire_finished([fact], 'example', tmp_path/'state', proc=proc, run=changed, now=NOW) == []
    assert not any(c[1] == 'send-keys' for c in calls)


def test_quiet_blocked_context_is_preserved_without_changing_ticket_state(tmp_path):
    import os
    proc, fact, calls, run = fixture(tmp_path)
    fact['meta'].update(build_state='blocked', block_reason='Missing dependency interface')
    fact['meta'].pop('finished_at')
    rollout=(proc/'12/fd/3').readlink()
    os.utime(rollout,(NOW.timestamp()-60,NOW.timestamp()-60))
    assert module.retire_finished([fact], 'example', tmp_path/'state', proc=proc, run=run, now=NOW)==['R007']
    record=json.loads((tmp_path/'state/retired-workers/R007.json').read_text())
    assert record['terminal_state']=='blocked'
    assert record['codex_session_id']=='resumable-session-id'
    assert fact['meta']['build_state']=='blocked'


def test_blocked_ticket_with_recent_activity_or_missing_reason_stays_alive(tmp_path):
    import os
    proc, fact, calls, run = fixture(tmp_path)
    fact['meta'].update(build_state='blocked', block_reason='Missing dependency interface')
    fact['meta'].pop('finished_at')
    rollout=(proc/'12/fd/3').readlink()
    os.utime(rollout,(NOW.timestamp()-1,NOW.timestamp()-1))
    assert module.retire_finished([fact], 'example', tmp_path/'state', proc=proc, run=run, now=NOW)==[]
    fact['meta'].pop('block_reason')
    os.utime(rollout,(NOW.timestamp()-60,NOW.timestamp()-60))
    assert module.retire_finished([fact], 'example', tmp_path/'state', proc=proc, run=run, now=NOW)==[]
    assert not any(c[1]=='send-keys' for c in calls)
