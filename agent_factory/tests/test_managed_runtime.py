"""Exercise a newly installed runtime, not a copy of its launch implementation."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from af_runtime_config import EXECUTION_POLICY, private_json, runtime_environment, settings
from af_runtime_entry import command_kind, prepare_node
from af_launch_prompt import prepare
from af_retire_claude import context, idle
from af_worker_metrics import measure
from af_wsl_host import powershell_command
import af_audit_supervise as audit_supervisor

spec = importlib.util.spec_from_file_location('runtime_setup', SCRIPTS / 'af-runtime-setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)
auth_spec = importlib.util.spec_from_file_location('runtime_auth', SCRIPTS / 'af-claude-auth.py')
auth = importlib.util.module_from_spec(auth_spec)
auth_spec.loader.exec_module(auth)


@pytest.fixture
def factory(tmp_path):
    source = tmp_path / 'source'
    (source / 'scripts').mkdir(parents=True)
    for path in SCRIPTS.glob('*.py'):
        shutil.copyfile(path, source / 'scripts' / path.name)
    (source / '.env.secret').write_text('NEVER-SNAPSHOT-THIS')
    yield source
    # The product snapshots are deliberately read-only, including their directories.
    for path in tmp_path.rglob('*'):
        if path.is_dir():
            path.chmod(0o700)


def values(tmp_path, name='alpha'):
    tree = tmp_path / (name + ' worktree')
    tree.mkdir()
    (tree / '.claude').mkdir()
    (tree / '.claude/settings.local.json').write_text(json.dumps({
        'env': {'FACTORY_PROJECT': 'stale-project', 'PRAXIS_API_KEY': 'synthetic-test-key',
                'PRAXIS_ORG': 'test-org'}, 'preserved': True}))
    return dict(project=name, worktree=str(tree), runtime=str(tmp_path / (name + ' runtime')),
                python=sys.executable, real_node=sys.executable,
                real_claude='/usr/bin/false', real_codex='/usr/bin/false',
                claude_config_dir=str(tmp_path / 'claude-login'))


def test_fresh_setups_are_isolated_private_and_reproducible(tmp_path, factory):
    a = setup.install(values(tmp_path), factory, unit_dir=tmp_path / 'units')
    b = setup.install(values(tmp_path, 'beta'), factory)
    assert a['factory'] != b['factory']
    assert Path(a['factory']).name == Path(b['factory']).name
    assert not (Path(a['factory']) / '.env.secret').exists()
    assert Path(a['runtime']).stat().st_mode & 0o077 == 0
    assert Path(a['environment_file']).stat().st_mode & 0o077 == 0
    assert (Path(a['factory']) / 'scripts/af_runtime_entry.py').stat().st_mode & 0o222 == 0
    pinned = json.loads((Path(a['worktree']) / '.claude/settings.local.json').read_text())
    assert pinned['preserved'] is True
    assert pinned['env']['FACTORY_PROJECT'] == 'alpha'
    assert pinned['env']['AF_RUNTIME_CONFIG'] != json.loads(
        (Path(b['worktree']) / '.claude/settings.local.json').read_text())['env']['AF_RUNTIME_CONFIG']
    for unit in (tmp_path / 'units').glob('*'):
        assert 'synthetic-test-key' not in unit.read_text()
    assert 'synthetic-test-key' not in (Path(a['runtime']) / 'config.json').read_text()


def test_generated_python_entry_loads_identity_and_pins_project(tmp_path, factory):
    config = setup.install(values(tmp_path), factory, pin_project=False)
    command = [str(Path(config['runtime']) / 'bin/factory-python'), '-c',
               'import os,json;print(json.dumps({k:os.environ[k] for k in ["FACTORY_PROJECT","AF_MODEL_BACKEND","PRAXIS_ORG"]}))']
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {'FACTORY_PROJECT': 'alpha', 'AF_MODEL_BACKEND': 'sonnet', 'PRAXIS_ORG': 'test-org'}
    assert 'synthetic-test-key' not in result.stdout + result.stderr


def test_generated_python_entry_can_import_shipped_hooks_package(tmp_path, factory):
    # Source snapshots ship hooks as a sibling package to src/agent_factory.
    # The source-layout command must work without a globally installed wheel.
    hooks = factory / 'hooks'
    hooks.mkdir()
    (hooks / '__init__.py').write_text('SNAPSHOT_PROOF = "owned-release-hooks"\n')
    config = setup.install(values(tmp_path), factory, pin_project=False)
    result = subprocess.run([str(Path(config['runtime']) / 'bin/factory-python'), '-c',
                             'import hooks; print(hooks.SNAPSHOT_PROOF)'],
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'owned-release-hooks'


def test_generation_probe_keeps_its_minimal_config_but_workers_use_pinned_login(tmp_path, factory):
    config = setup.install(values(tmp_path), factory, pin_project=False)
    normal = runtime_environment(config, {'CLAUDE_CONFIG_DIR': '/other-login'})
    assert normal['CLAUDE_CONFIG_DIR'] == config['claude_config_dir']
    probe = runtime_environment(config, {'AF_RUNTIME_PREFLIGHT': '1', 'CLAUDE_CONFIG_DIR': '/private/probe'})
    assert probe['CLAUDE_CONFIG_DIR'] == '/private/probe'


def test_generated_node_guard_rejects_census_before_executing_it(tmp_path, factory):
    config = setup.install(values(tmp_path), factory, pin_project=False)
    result = subprocess.run([str(Path(config['runtime']) / 'bin/node'), 'vitest', 'run'],
                            capture_output=True, text=True)
    assert result.returncode == 64
    assert 'No check ran or passed' in result.stderr


def audit_values(tmp_path, backend='codex'):
    raw = values(tmp_path)
    prompt = tmp_path / 'assignment.md'
    prompt.write_text('Audit the assigned deployed target with isolated fixtures.')
    return {**raw, 'mode': 'audit', 'backend': backend, 'model': 'existing-model',
            'audit_run': 'test-run', 'audit_prompt_file': str(prompt),
            'audit_phases': ['audit', 'reaudit'], 'audit_wait_phases': ['fix', 'deploy'],
            'audit_browser_probe': [sys.executable, '-c', 'pass']}


@pytest.mark.parametrize('backend,engine', [('codex', 'codex'), ('sonnet', 'claude')])
def test_audit_install_uses_shared_runtime_and_engine_without_forcing_model(tmp_path, factory, backend, engine):
    raw = audit_values(tmp_path, backend)
    config = setup.install(raw, factory, pin_project=False)
    command = audit_supervisor.agent_command(config, tmp_path / 'prompt', tmp_path / 'output')
    assert command[command.index('--engine') + 1] == engine
    assert command[command.index('--cli-path') + 1] == str(Path(config['runtime']) / 'bin' / engine)
    assert '--model' not in command
    assert config['model'] == 'existing-model'
    assert config['service'] == 'praxis-af-alpha-audit-test-run.service'
    assert Path(config['audit_prompt_file']).read_text() == Path(raw['audit_prompt_file']).read_text()
    assert Path(config['audit_prompt_file']).stat().st_mode & 0o077 == 0
    assert 'RestartPreventExitStatus=78' in setup.units(config)[config['service']]


def test_audit_profile_refuses_ambiguous_phase_and_shell_probe(tmp_path):
    raw = {**audit_values(tmp_path), 'factory': '/factory', 'environment_file': '/private/env'}
    with pytest.raises(ValueError, match='disjoint'):
        settings({**raw, 'audit_wait_phases': ['audit']})
    with pytest.raises(ValueError, match='argv array'):
        settings({**raw, 'audit_browser_probe': 'echo success'})
    with pytest.raises(ValueError, match='audit_run'):
        settings({**raw, 'audit_run': '../other-run'})


def test_audit_supervisor_does_not_treat_successful_process_as_coverage(tmp_path, monkeypatch):
    config = audit_values(tmp_path)
    config.update(factory='/factory', audit_no_progress_limit=2)
    canonical = {'project': 'alpha', 'run': 'test-run', 'phase': 'audit', 'counts': {'not_run': 2}}
    monkeypatch.setattr(audit_supervisor, 'status', lambda *_: canonical)
    monkeypatch.setattr(audit_supervisor, 'plan_assignment', lambda *_: {
        'identity_available': False, 'dependency': {}, 'runnable_original_ids': ['public-one'],
        'deferred_identity_ids': [], 'reserved_ids': [], 'no_runnable_originals': False})
    turns = []
    monkeypatch.setattr(audit_supervisor, 'run_turn', lambda *args: turns.append(args) or 0)
    assert audit_supervisor.supervise(config, {'PRAXIS_ORG': 'test-org'}, sleep=lambda _: None) == 78
    assert len(turns) == 2
    assert canonical['counts']['not_run'] == 2


def test_audit_supervisor_waits_for_local_fix_then_reaudits(tmp_path, monkeypatch):
    config = audit_values(tmp_path)
    config.update(factory='/factory', audit_no_progress_limit=2)
    states = iter([{'phase': 'fix'}, {'phase': 'deploy'}, {'phase': 'reaudit'},
                   {'phase': 'complete'}, {'phase': 'complete'}])
    monkeypatch.setattr(audit_supervisor, 'status', lambda *_: next(states))
    turns, waits = [], []
    monkeypatch.setattr(audit_supervisor, 'run_turn', lambda *args: turns.append(args[3]) or 0)
    assert audit_supervisor.supervise(config, {'PRAXIS_ORG': 'test-org'}, sleep=waits.append) == 0
    assert turns == ['reaudit']
    assert waits == [30, 30]


def test_audit_status_unavailable_fails_closed(monkeypatch):
    config = {'python': sys.executable, 'project': 'alpha', 'audit_run': 'test-run', 'worktree': '/tmp'}
    monkeypatch.setattr(audit_supervisor.subprocess, 'run', lambda *_, **__: SimpleNamespace(returncode=2))
    with pytest.raises(RuntimeError, match='no audit completion'):
        audit_supervisor.status(config, {'PRAXIS_ORG': 'test-org'})


def test_audit_preflight_requires_actual_file_tools_after_model_exit(tmp_path, monkeypatch):
    config = audit_values(tmp_path)
    monkeypatch.setattr(audit_supervisor, 'status', lambda *_: {'phase': 'audit'})
    monkeypatch.setattr(audit_supervisor.subprocess, 'run', lambda *_, **__: SimpleNamespace(returncode=0))
    monkeypatch.setattr(audit_supervisor, 'run_turn', lambda *_: 0)
    with pytest.raises(RuntimeError, match='read/write preflight failed'):
        audit_supervisor.preflight(config, {'PRAXIS_ORG': 'test-org'})


def test_audit_preflight_accepts_verified_file_roundtrip_and_browser(tmp_path, monkeypatch):
    import re
    config = audit_values(tmp_path)
    monkeypatch.setattr(audit_supervisor, 'status', lambda *_: {'phase': 'audit'})
    calls = []
    def turn(_config, env, prompt, label):
        source = re.search(r'Read (.+?) using a file tool', prompt)[1]
        destination = re.search(r'contents to (.+?) using a file tool', prompt)[1]
        Path(destination).write_text(Path(source).read_text())
        calls.append((env['AF_RUNTIME_PREFLIGHT'], label))
        return 0
    monkeypatch.setattr(audit_supervisor, 'run_turn', turn)
    # Executes the configured harmless local probe as a real child process.
    assert audit_supervisor.preflight(config, {'PRAXIS_ORG': 'test-org'}) == 0
    assert calls == [('1', 'preflight')]
    assert not list(Path(config['worktree']).glob('.af-audit-probe-*'))


def test_live_worktree_lock_prevents_any_repinning(tmp_path, factory):
    original = values(tmp_path)
    tree = Path(original['worktree'])
    before = (tree / '.claude/settings.local.json').read_bytes()
    (tree / '.af-loop.lock').write_text(f'{os.getpid()} alpha\n')
    with pytest.raises(ValueError, match='driver lock'):
        setup.install(original, factory)
    assert (tree / '.claude/settings.local.json').read_bytes() == before
    assert not Path(original['runtime']).exists()


def test_snapshot_uses_committed_release_and_preserves_prior_image(tmp_path):
    source = tmp_path / 'git-source'
    (source / 'scripts').mkdir(parents=True)
    entry = source / 'scripts/af_runtime_entry.py'
    entry.write_text('# committed\n')
    def git(*args):
        return subprocess.run(['git', '-C', str(source), *args], check=True, capture_output=True)
    git('init', '-q')
    git('config', 'user.email', 'test@example.invalid')
    git('config', 'user.name', 'Test')
    git('add', '.')
    git('commit', '-qm', 'release')
    a = setup.snapshot(source, tmp_path / 'runtime')
    entry.write_text('# uncommitted must not leak into launch\n')
    b = setup.snapshot(source, tmp_path / 'runtime')
    assert a == b
    assert (a / 'scripts/af_runtime_entry.py').read_text() == '# committed\n'
    for path in (a, a / 'scripts'):
        path.chmod(0o700)


def test_model_scope_is_applied_to_all_roles_without_slash_dispatch():
    for prompt in ['/af-build shop R001', 'Resolve MERGE CONFLICTS', 'Post-merge verification']:
        result = prepare(prompt, backend='sonnet', factory=Path('/factory'), policy='Payments only.')
        assert not result.startswith('/')
        assert prompt in result and 'Payments only.' in result
        assert '/factory/skills/af-build/SKILL.md' in result


def test_quality_commands_use_small_lane_while_builds_remain_large(tmp_path):
    raw = values(tmp_path)
    raw.update(factory='/factory', environment_file='/private/env.json')
    config = settings(raw)
    assert command_kind(['check-authored-types.ts'], config) == (True, 'unit')
    assert command_kind(['npm', 'run', 'build'], config) == (True, 'build')
    assert command_kind(['test-harness.ts', '--', 'npm', 'run', 'test:integration'], config) == (True, 'api')
    assert settings(config)['scope_policy'].count(EXECUTION_POLICY) == 1
    env = {'TICKET_BASE': 'recorded-base'}
    assert prepare_node(['check-ticket-types.ts'], config, env)[-2:] == ['--base', 'recorded-base']


def test_browser_defaults_only_remove_inherited_matching_values(tmp_path):
    path = tmp_path / 'fixture-defaults.json'
    setup.private_write(path, json.dumps({'ORIGIN': 'application-default'}))
    config = {**setup.settings({**values(tmp_path), 'factory': '/factory', 'environment_file': '/private/env'}),
              'fixture_defaults_file': str(path), 'fixture_default_keys': ['ORIGIN']}
    args = ['playwright', 'test', '--config', 'playwright.checkout.config.js']
    inherited = {'ORIGIN': 'application-default'}
    prepare_node(args, config, inherited)
    assert 'ORIGIN' not in inherited
    explicit = {'ORIGIN': 'test-override'}
    prepare_node(args, config, explicit)
    assert explicit['ORIGIN'] == 'test-override'


def test_scope_directories_are_project_configuration(tmp_path):
    raw = {**values(tmp_path), 'factory': '/factory', 'environment_file': '/private/env',
           'selected_test_directories': ['src/payments/'], 'excluded_commands': ['test:media']}
    config = settings(raw)
    assert prepare_node(['vitest', 'run', 'src/payments/'], config, {})
    with pytest.raises(ValueError, match='excluded'):
        prepare_node(['npm', 'run', 'test:media'], config, {})
    with pytest.raises(ValueError, match='Select explicit'):
        prepare_node(['vitest', 'run', 'src/features/league/'], config, {})


def test_invalid_resource_profile_and_exposed_env_are_rejected(tmp_path):
    raw = {**values(tmp_path), 'factory': '/factory', 'environment_file': '/private/env'}
    with pytest.raises(ValueError, match='headroom'):
        settings({**raw, 'memory_high_gib': 6, 'memory_max_gib': 7})
    exposed = tmp_path / 'exposed.json'
    exposed.write_text('{}')
    exposed.chmod(0o644)
    with pytest.raises(ValueError, match='owner-only'):
        private_json(exposed)


def test_claude_auth_import_rejects_mcp_only_identity_and_never_prints_secrets(tmp_path, capsys):
    with pytest.raises(ValueError, match='MCP-only'):
        auth.install(json.dumps({'mcpOAuth': {}}), tmp_path / 'auth')
    import time
    auth.install(json.dumps({'claudeAiOauth': {'accessToken': 'synthetic-access',
                  'refreshToken': 'synthetic-refresh', 'subscriptionType': 'max',
                  'expiresAt': time.time() * 1000 + 60000}}), tmp_path / 'auth')
    assert (tmp_path / 'auth/.credentials.json').stat().st_mode & 0o077 == 0
    assert 'synthetic-access' not in capsys.readouterr().out


def test_completed_claude_context_is_identified_and_busy_footer_is_not_idle(tmp_path):
    root = tmp_path / 'projects/test'
    root.mkdir(parents=True)
    session = '00000000-0000-4000-8000-000000000001'
    (root / (session + '.jsonl')).write_text(json.dumps(dict(type='user', sessionId=session,
        message={'content': 'Execute assignment /af-build shop R001 — build exactly this ONE ticket'})) + '\n')
    assert context(tmp_path, 'shop', 'R001')[0] == session
    assert context(tmp_path, 'another-project', 'R001') is None
    assert idle('done\nbypass permissions on')
    for suffix in ['esc to interrupt', '1 shell', '2 monitors', 'queued message']:
        assert not idle('bypass permissions on · ' + suffix)


@pytest.mark.parametrize('busy,foreign,valid,expected', [
    (False, False, True, ['R1']), (True, False, True, []),
    (False, True, True, []), (False, False, False, [])])
def test_claude_scope_retirement_requires_idle_validated_owned_context(tmp_path, busy, foreign, valid, expected):
    from af_retire_claude import retire
    root = tmp_path / 'proc'
    for pid, comm, children, args in [
        ('100', 'bash', '101', b'bash\0'),
        ('101', 'claude', '102 103' if busy else '102', b'claude\0'),
        ('102', 'python', '', b'python\0-B\0-m\0knowledge.mcp\0'),
        ('103', 'node', '', b'node\0vitest\0')]:
        path = root / pid
        (path / 'task' / pid).mkdir(parents=True)
        (path / 'task' / pid / 'children').write_text(children)
        (path / 'comm').write_text(comm)
        (path / 'cmdline').write_bytes(args)
        (path / 'cgroup').write_text('0::/user.slice/owned.scope\n')
    login = tmp_path / 'login'
    session = '00000000-0000-4000-8000-000000000001'
    transcript = login / 'projects/p' / (session + '.jsonl')
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps(dict(type='user', sessionId=session,
        message={'content': '/af-build shop R1 — build exactly this ONE ticket'})) + '\n')
    config = dict(project='shop', session_prefix='af-shop', runtime=str(tmp_path / 'runtime'),
                  claude_config_dir=str(login), service='praxis-af-shop.service')
    stopped = []
    def run(command, **_):
        if 'display-message' in command:
            return SimpleNamespace(stdout='claude')
        if 'capture-pane' in command:
            return SimpleNamespace(stdout='done\nbypass permissions on')
        if 'list-panes' in command:
            return SimpleNamespace(stdout='100')
        if 'show' in command:
            return SimpleNamespace(stdout='another.service' if foreign else config['service'])
        if 'stop' in command:
            receipt = Path(config['runtime']) / 'state/retired-workers/R1-claude.json'
            assert json.loads(receipt.read_text())['claude_session_id'] == session
            stopped.append(command[-1])
        return SimpleNamespace(stdout='')
    facts = [dict(meta=dict(requirement_id='R1', build_state='finished'))]
    assert retire(facts, config, lambda _: valid, run=run, proc=root) == expected
    assert stopped == (['owned.scope'] if expected else [])


def test_timing_keeps_original_launch_across_resume_and_reopen():
    facts = [{'meta': {'requirement_id': 'R1', 'build_state': 'finished',
                      'finished_at': '1970-01-01T00:30:00+00:00'}}]
    events = [dict(event='worker_launch', ticket='R1', at=0),
              dict(event='worker_launch', ticket='R1', at=1200)]
    row = measure(facts, events, 2000)['tickets'][0]
    assert row['minutes'] == 30 and row['above_target'] is True


def test_windows_keepalive_is_owned_and_quotes_paths_without_secrets(tmp_path):
    config = dict(project='shop', service='praxis-af-shop.service',
                  factory='/home/user/factory with spaces', python='/usr/bin/python3')
    command = powershell_command(config, 'Ubuntu')
    assert 'praxis-af-shop.service' in command
    assert 'factory with spaces' in command and '-RedirectStandardOutput' in command
    assert 'PRAXIS_API_KEY' not in command


def test_service_working_directory_is_absolute_directive_not_argv():
    config = dict(service='praxis-af-example.service', slice='praxis-af-example.slice',
                  project='example', runtime='/private/runtime', worktree='/workspace/two words',
                  memory_high_gib=8, memory_max_gib=12)
    unit = setup.units(config)[config['service']]
    assert 'WorkingDirectory=/workspace/two words\n' in unit


def test_audit_supervisor_defers_shared_identity_once_without_model_redirect_churn(tmp_path, monkeypatch):
    config = audit_values(tmp_path)
    config.update(factory='/factory')
    canonical = {'phase': 'audit', 'counts': {'blocked': 12}}
    monkeypatch.setattr(audit_supervisor, 'status', lambda *_: canonical)
    monkeypatch.setattr(audit_supervisor, 'plan_assignment', lambda *_: {
        'identity_available': False, 'dependency': {'actionable_blocker': 'Configure project provisioner'},
        'runnable_original_ids': [], 'deferred_identity_ids': ['protected-one'],
        'reserved_ids': [], 'no_runnable_originals': True})
    monkeypatch.setattr(audit_supervisor, 'run_turn', lambda *_: pytest.fail('no anonymous redirect turn expected'))
    assert audit_supervisor.supervise(config, {'PRAXIS_ORG': 'test-org'}) == 78


def test_identity_profile_commands_are_operator_argv_and_limits_are_positive(tmp_path):
    config = audit_values(tmp_path)
    config.update(factory='/factory', environment_file='/private/env.json')
    config['audit_identity_probe'] = 'curl https://served-website.example/command'
    with pytest.raises(ValueError, match='argv'):
        settings(config)
    config['audit_identity_probe'] = ['/usr/bin/python3', '/owned/probe.py']
    config['audit_identity_provisioner'] = ['/usr/bin/python3', '/owned/provision.py']
    config['audit_identity_max_attempts'] = 0
    with pytest.raises(ValueError, match='positive'):
        settings(config)
    config['audit_identity_max_attempts'] = 2
    assert settings(config)['audit_identity_max_attempts'] == 2


def test_prerequisite_handler_uses_same_scoped_environment_as_status(tmp_path, monkeypatch):
    config = audit_values(tmp_path)
    config.update(factory='/factory')
    env = {'PRAXIS_ORG': 'specific-org', 'PRAXIS_API_KEY': 'synthetic-private-key'}
    canonical = {'phase': 'audit', 'run_meta': {}}
    captured = []
    def execute(command, **kwargs):
        captured.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({'identity_available': True}))
    monkeypatch.setattr(audit_supervisor.subprocess, 'run', execute)
    assert audit_supervisor.plan_assignment(config, canonical, env)['identity_available']
    command, options = captured[0]
    assert command[0] == config['python']
    assert options['env'] is env
    assert json.loads(options['input'])['expected_org'] == 'specific-org'
    assert 'synthetic-private-key' not in options['input']


def test_identity_wait_reprobes_and_then_runs_without_anonymous_turn(tmp_path, monkeypatch):
    config = audit_values(tmp_path)
    config.update(factory='/factory', audit_no_progress_limit=2)
    states = iter([{'phase': 'audit'}, {'phase': 'audit'}, {'phase': 'complete'}, {'phase': 'complete'}])
    monkeypatch.setattr(audit_supervisor, 'status', lambda *_: next(states))
    plans = iter([{'no_runnable_originals': True, 'deferred_identity_ids': ['AUTH'], 'wait_for_identity': True,
                   'dependency': {'actionable_blocker': 'Await project provisioner', 'attempts': 1}},
                  {'no_runnable_originals': False, 'deferred_identity_ids': [], 'dependency': {},
                   'runnable_original_ids': ['AUTH']}])
    monkeypatch.setattr(audit_supervisor, 'plan_assignment', lambda *_: next(plans))
    waits, turns = [], []
    monkeypatch.setattr(audit_supervisor, 'run_turn', lambda *args: turns.append(args[2]) or 0)
    assert audit_supervisor.supervise(config, {'PRAXIS_ORG': 'specific-org'}, sleep=waits.append) == 0
    assert waits == [30]
    assert len(turns) == 1
    assert 'AUTH' in turns[0]
