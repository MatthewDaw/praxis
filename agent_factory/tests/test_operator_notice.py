import hashlib
import importlib.util
import json
from pathlib import Path
import time

import pytest

FILE = Path(__file__).resolve().parents[1]/'hooks/_operator_notice.py'
spec = importlib.util.spec_from_file_location('notice', FILE)
notice = importlib.util.module_from_spec(spec); spec.loader.exec_module(notice)


@pytest.fixture
def note(tmp_path, monkeypatch):
    monkeypatch.setenv('AF_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('FACTORY_PROJECT', 'example')
    monkeypatch.chdir(tmp_path)
    directory = tmp_path/'operator-notices';directory.mkdir()
    key = hashlib.sha256(b'example:ticket').hexdigest()
    path = directory/(key+'.json')
    path.write_text(json.dumps({'project':'example','cid':'ticket','owner':'worker',
        'cwd_prefix':str(tmp_path),'expires_at':time.time()+600,'message':'Keep active tests running.'}))
    path.chmod(0o600)
    return path


def test_note_delivered_once_only_for_live_owner_context(note, capsys):
    assert notice.deliver_once('ticket', 'worker')
    assert 'Keep active tests running.' in capsys.readouterr().err
    assert not notice.deliver_once('ticket', 'worker')
    assert note.with_suffix('.delivered').exists()


@pytest.mark.parametrize('change', [{'owner':'other'}, {'project':'other'}, {'cid':'other'},
    {'cwd_prefix':'/another/checkout/'}, {'expires_at':0}, {'message':''}])
def test_mismatch_does_not_consume_notice(note, change):
    data=json.loads(note.read_text());data.update(change);note.write_text(json.dumps(data))
    assert not notice.deliver_once('ticket','worker')
    assert note.exists()


def test_public_or_invalid_notice_is_ignored(note):
    note.chmod(0o644)
    assert not notice.deliver_once('ticket','worker')
    note.chmod(0o600);note.write_text('[]')
    assert not notice.deliver_once('ticket','worker')


def test_hook_delivers_only_after_live_lease_guard_and_successful_heartbeat():
    code=(FILE.parent/'_ticket_state.py').read_text().split('def heartbeat(',1)[1].split('def uncommitted_changes(',1)[0]
    assert code.index('not _lease_live(meta)') < code.index('write_build_state') < code.index('deliver_once(cid, owner)')
