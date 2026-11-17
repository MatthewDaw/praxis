"""Static, project-owned settings for unattended factory runs.

Credentials stay in a private input file. This module never stores ticket status;
Praxis remains authoritative for claims, completion and validation outcomes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re


GIB = 1024**3
EXECUTION_POLICY = (
    'Use affected acceptance, module and caller checks per ticket. At integration verify the union '
    'of landed changes and affected runtime authorization, isolation and persistence. Do not run '
    'unrelated legacy test/type/lint censuses. Keep explicitly required production builds and '
    'bundle limits. Correct overbroad check contracts explicitly; never label a scoped run a '
    'whole-suite pass. Reuse completed evidence only when its actual inputs match. Advisory '
    'reviews retain honest outcomes but do not reopen otherwise complete tickets. Keep original '
    'ticket clocks and resumable conversations. Aim for 5–10 minutes of checks and under20 minutes '
    'per ticket without using time limits to weaken required coverage.'
)
DEFAULTS = {
    'mode': 'build',
    'backend': 'sonnet', 'model': 'sonnet', 'batch_max': 8,
    'memory_high_gib': 8, 'memory_max_gib': 12,
    'unit_memory_gib': 3, 'build_memory_gib': 6,
    'host_available_gib': 8, 'memory_margin_gib': 1,
    'pg_port': 5432, 'redis_port': 'none',
    'scope_policy': '', 'selected_test_directories': [],
    'excluded_commands': [], 'unit_scripts': ['test', 'test:ticket'],
    'unit_commands': ['check-ticket-types.ts', 'check-authored-types.ts',
                      'grade-authored-diff.ts', 'af-check-ticket-types.mjs'],
    'fixture_default_keys': [], 'fixture_defaults_file': None,
    'application_env_file': None,
    'scoped_types_commands': ['check-ticket-types.ts'],
    'audit_phases': ['audit', 'reaudit'], 'audit_wait_phases': ['fix', 'deploy'],
    'audit_batch_timeout_s': 1800, 'audit_no_progress_limit': 3,
}


def settings(values: dict) -> dict:
    result = {**DEFAULTS, **values}
    if not result['scope_policy'].startswith(EXECUTION_POLICY):
        result['scope_policy'] = EXECUTION_POLICY + '\n' + result['scope_policy']
    project = result.get('project', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', project):
        raise ValueError('project must be a bare Praxis project name')
    if result['backend'] not in {'sonnet', 'codex'}:
        raise ValueError('managed runtime supports sonnet or codex')
    if result['mode'] not in {'build', 'audit'}:
        raise ValueError('runtime mode must be build or audit')
    if result['mode'] == 'audit':
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', result.get('audit_run', '')):
            raise ValueError('audit_run must name an explicit Praxis run')
        if not Path(result.get('audit_prompt_file', '')).is_absolute():
            raise ValueError('audit_prompt_file must be an absolute path')
        phases = set(result['audit_phases'])
        waits = set(result['audit_wait_phases'])
        if not phases or not phases <= {'audit', 'fix', 'deploy', 'reaudit'} or not waits <= {'audit', 'fix', 'deploy', 'reaudit'} or phases & waits:
            raise ValueError('audit phases and wait phases must be explicit and disjoint')
        probe = result.get('audit_browser_probe')
        if not isinstance(probe, list) or not probe or not all(isinstance(x, str) and x for x in probe) or not Path(probe[0]).is_absolute():
            raise ValueError('audit_browser_probe must be an argv array with an absolute executable')
        for key in ('audit_batch_timeout_s', 'audit_no_progress_limit'):
            if not isinstance(result[key], int) or isinstance(result[key], bool) or result[key] <= 0:
                raise ValueError(f'{key} must be a positive integer')
    for key in ('batch_max', 'memory_high_gib', 'memory_max_gib', 'unit_memory_gib',
                'build_memory_gib', 'host_available_gib', 'memory_margin_gib'):
        if not isinstance(result[key], int) or isinstance(result[key], bool) or result[key] <= 0:
            raise ValueError(f'{key} must be a positive integer')
    if result['memory_high_gib'] >= result['memory_max_gib']:
        raise ValueError('memory high must be below the hard limit')
    if result['build_memory_gib'] + result['memory_margin_gib'] >= result['memory_max_gib']:
        raise ValueError('hard limit must leave headroom beyond one build reservation')
    if result['unit_memory_gib'] > result['build_memory_gib']:
        raise ValueError('unit reservation must not exceed the build reservation')
    for key in ('worktree', 'runtime', 'factory', 'environment_file', 'python', 'real_node'):
        if key not in result or not Path(result[key]).is_absolute():
            raise ValueError(f'{key} must be an absolute path')
    if result['backend'] == 'sonnet' and not result.get('claude_config_dir'):
        raise ValueError('Sonnet requires an existing authenticated Claude config directory')
    unit_project = project + '-audit-' + result['audit_run'] if result['mode'] == 'audit' else project
    result['service'] = f'praxis-af-{unit_project}.service'
    result['slice'] = f'praxis-af-{unit_project}.slice'
    result['session_prefix'] = 'af-' + Path(result['worktree']).name
    return result


def private_json(path: Path) -> dict:
    stat = path.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError(f'private owner-only file required: {path}')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f'JSON object required: {path}')
    return value


def user_bus_environment(inherited: dict[str, str]) -> dict[str, str]:
    env = dict(inherited)
    bus = Path('/run/user') / str(os.getuid())
    if (bus / 'bus').exists() and bus.stat().st_uid == os.getuid():
        env.setdefault('XDG_RUNTIME_DIR', str(bus))
        env.setdefault('DBUS_SESSION_BUS_ADDRESS', 'unix:path=' + str(bus / 'bus'))
    return env


def runtime_environment(config: dict, inherited: dict[str, str]) -> dict[str, str]:
    env = user_bus_environment(inherited)
    supplied = private_json(Path(config['environment_file']))
    env.update({k: str(v) for k, v in supplied.items()})
    if config.get('application_env_file'):
        env.update({k: str(v) for k, v in private_json(Path(config['application_env_file'])).items()})
    factory, runtime = Path(config['factory']), Path(config['runtime'])
    # Pin tenancy/project after loading the input: a copied stale project cannot win.
    env.update(FACTORY_PROJECT=config['project'], AF_PLUGIN_DIR=str(factory),
               AF_STATE_DIR=str(runtime / 'state'), AF_SERVICE_UNIT=config['service'],
               AF_SESSION_PREFIX=config['session_prefix'], AF_PYTHON=config['python'],
               PRAXIS_HOOK_PYTHON=config['python'], AF_MODEL_BACKEND=config['backend'],
               AF_CLAUDE_MODEL=config['model'], AF_BATCH_MAX=str(config['batch_max']),
               AF_ROLLING='1', AF_FAST_VERIFY='1', AF_DEPENDENCIES_ADVISORY='1',
               AF_LEAN_WORKERS='1', AF_TICKET_DEADLINE_ACTION='warn',
               AF_RUNTIME_CONFIG=str(runtime / 'config.json'))
    env['PATH'] = str(runtime / 'bin') + os.pathsep + env.get('PATH', '')
    env['PYTHONPATH'] = os.pathsep.join([str(factory), str(factory / 'scripts'), str(factory / 'hooks'),
                                       str(factory / 'src'), env.get('PYTHONPATH', '')])
    if config.get('claude_config_dir'):
        env['CLAUDE_CONFIG_DIR'] = (inherited.get('CLAUDE_CONFIG_DIR', config['claude_config_dir'])
                                   if inherited.get('AF_RUNTIME_PREFLIGHT') else config['claude_config_dir'])
    if config.get('mcp_config'):
        env['AF_RUNTIME_MCP_CONFIG'] = config['mcp_config']
    env['AF_OWNER_EXECUTION_POLICY'] = config['scope_policy']
    return env
