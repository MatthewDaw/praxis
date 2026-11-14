from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path
from types import SimpleNamespace
import pytest

spec = spec_from_file_location('scope_exec', Path(__file__).resolve().parents[1] / 'scripts/af-scope-exec.py')
module = module_from_spec(spec)
spec.loader.exec_module(module)


def test_scope_stops_with_its_service_and_preserves_exact_command(monkeypatch):
    calls = []
    monkeypatch.setattr(module.subprocess, 'run', lambda argv: (calls.append(argv) or SimpleNamespace(returncode=0)))
    command = module.scope_command('own-test', 'own.slice', ['node', 'a file.js', '--bail=1'], owner='own.service', memory_max='8G')
    assert '--property=BindsTo=own.service' in command
    assert '--property=After=own.service' in command
    assert '--property=MemoryMax=8G' in command
    assert command[-4:] == ['--', 'node', 'a file.js', '--bail=1']
    assert calls == [['systemctl', '--user', 'is-active', '--quiet', 'own.service']]


def test_orphan_cannot_start_owner_or_launch_without_it(monkeypatch):
    monkeypatch.setattr(module.subprocess, 'run', lambda argv: SimpleNamespace(returncode=3))
    with pytest.raises(RuntimeError, match='inactive'):
        module.scope_command('own-test', 'own.slice', ['node', 'test.js'], owner='own.service')


def test_cleanup_stops_only_scopes_explicitly_bound_to_this_service(monkeypatch):
    spec = spec_from_file_location('scope_cleanup', Path(__file__).resolve().parents[1] / 'scripts/af-stop-owned-scopes.py')
    cleanup = module_from_spec(spec)
    spec.loader.exec_module(cleanup)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        if 'list-units' in argv:
            return SimpleNamespace(stdout='own.scope loaded active running\nother.scope loaded active running\n')
        if 'show' in argv:
            return SimpleNamespace(stdout='own.service' if 'own.scope' in argv else 'other.service')
        return SimpleNamespace(stdout='')
    monkeypatch.setenv('AF_SERVICE_UNIT', 'own.service')
    monkeypatch.setattr(cleanup.subprocess, 'run', run)
    assert cleanup.main() == 0
    assert ['systemctl', '--user', 'stop', 'own.scope'] in calls
    assert ['systemctl', '--user', 'stop', 'other.scope'] not in calls
