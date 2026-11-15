#!/usr/bin/env python3
"""Managed runtime entry points: guarded commands, model launch and supervision."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from af_runtime_config import GIB, private_json, runtime_environment, settings


def command_kind(args: list[str], config: dict) -> tuple[bool, str]:
    from af_validation_slots import validation_kind
    if not args:
        return False, 'unit'
    name = Path(args[0]).name
    words = args[1:]
    npm = name in {'npm', 'npm-cli.js', 'npx', 'npx-cli.js'}
    heavy = (npm and any(x in {'ci', 'install', 'test', 'build', 'verify', 'typecheck', 'lint'}
                         or x.startswith(('test:', 'build:', 'verify:', 'check:')) for x in words))
    heavy = heavy or name.startswith(('vitest', 'check-', 'test-', 'verify-', 'af-check-',
                                      'tsc', '_tsc', 'eslint', 'prettier', 'playwright'))
    heavy = heavy or (name.startswith('vite') and 'build' in words)
    kind = validation_kind(name, words, unit_scripts=set(config['unit_scripts']),
                           unit_commands=set(config['unit_commands']))
    return heavy, kind


def actual_build(args: list[str]) -> bool:
    if not args:
        return False
    name, words = Path(args[0]).name, args[1:]
    if name.startswith('vite'):
        return 'build' in words
    return name in {'npm', 'npm-cli.js', 'npx', 'npx-cli.js'} and 'run' in words and any(
        word == 'build' or word.startswith('build:') for word in words)


def prepare_node(args: list[str], config: dict, env: dict[str, str]) -> list[str]:
    from af_ticket_test_scope import rejection_reason
    if not args:
        return args
    build = actual_build(args)
    reason = rejection_reason(args, allow_project_types=build or bool(env.get('AF_BUILD_ACTIVE')),
                              ticket_base=env.get('TICKET_BASE', ''),
                              selected_directories=tuple(config['selected_test_directories']),
                              excluded_commands=tuple(config['excluded_commands']))
    if reason:
        raise ValueError(reason + ' No check ran or passed.')
    if build:
        env['AF_BUILD_ACTIVE'] = '1'
    if config['fixture_defaults_file'] and any(
            x == 'test:e2e' or x.startswith('playwright') for x in args):
        from af_test_fixture_env import without_inherited_defaults
        defaults = private_json(Path(config['fixture_defaults_file']))
        normalized, _ = without_inherited_defaults(env, defaults, tuple(config['fixture_default_keys']))
        env.clear()
        env.update(normalized)
    if Path(args[0]).name in config['scoped_types_commands']:
        base = env.get('TICKET_BASE', '')
        if '--base' in args:
            index = args.index('--base')
            if index + 1 < len(args):
                base = args[index + 1]
        if not base:
            raise ValueError('Set the recorded TICKET_BASE for authored TypeScript checks.')
        env['TICKET_BASE'] = base
        return [str(Path(config['factory']) / 'scripts/af-check-ticket-types.mjs'), '--base', base]
    return args


def scope(config: dict, command: list[str], kind: str | None = None, *, owner: bool = True) -> list[str]:
    args = [config['python'], str(Path(config['factory']) / 'scripts/af-scope-exec.py'),
            '--slice', config['slice'],
            '--unit', f'praxis-af-{config["project"]}-{os.getpid()}']
    if owner:
        args += ['--owner-unit', config['service']]
    if kind:
        size = config['build_memory_gib'] if kind == 'build' else config['unit_memory_gib']
        args += ['--memory-high', f'{max(1, size - 1)}G', '--memory-max', f'{size}G']
    return [*args, '--', *command]


def admit(config: dict, kind: str, env: dict[str, str]) -> None:
    from af_validation_slots import (acquire_weighted, reclaim_project_memory,
                                     reservation_fits, working_set_bytes)
    runtime = Path(config['runtime'])
    # Return descriptors stay inheritable across exec; keep Python references too.
    locks = acquire_weighted(runtime / 'state', integration=kind == 'api',
                             lightweight=kind == 'unit', cwd=Path.cwd())
    result = subprocess.run(['systemctl', '--user', 'show', config['slice'],
                             '--property=ControlGroup', '--value'], check=True,
                            capture_output=True, text=True)
    relative = result.stdout.strip()
    if not relative.startswith('/') or relative == '/':
        raise RuntimeError('project cgroup not available; refusing unbounded validation')
    cgroup = Path('/sys/fs/cgroup') / relative.lstrip('/')
    size = config['build_memory_gib'] if kind == 'build' else config['unit_memory_gib']
    queued = time.monotonic()
    while True:
        available = next(int(line.split()[1]) * 1024 for line in Path('/proc/meminfo').read_text().splitlines()
                         if line.startswith('MemAvailable:'))
        usage = working_set_bytes(cgroup)
        limit = int((cgroup / 'memory.max').read_text())
        if available >= config['host_available_gib'] * GIB and reservation_fits(
                usage, requested_bytes=size * GIB, limit_bytes=limit,
                margin_bytes=config['memory_margin_gib'] * GIB):
            break
        reclaim_project_memory(cgroup, runtime / 'state')
        time.sleep(5)
    with (runtime / 'state/validation-admission.jsonl').open('a') as stream:
        stream.write(json.dumps(dict(at=time.time(), pid=os.getpid(), kind=kind,
                                     queue_seconds=round(time.monotonic() - queued, 3),
                                     cwd=str(Path.cwd()))) + '\n')
    env['AF_VALIDATION_ACTIVE'] = str(os.getpid())
    # File descriptors intentionally survive scope-exec and the isolated check.
    assert locks


def maintenance(config: dict, env: dict[str, str]) -> int:
    scripts = Path(config['factory']) / 'scripts'
    for name in ('af_continue_idle_worker.py', 'af_retire_finished.py'):
        subprocess.run([config['python'], str(scripts / name), '--project', config['project'],
                        '--state-dir', str(Path(config['runtime']) / 'state'),
                        '--session-prefix', config['session_prefix']], env=env, check=True)
    import _praxis as praxis
    import _ticket_state as ticket_state
    from af_worker_metrics import observe
    facts = praxis.facts_by(category='requirement', space=config['project'],
                            snapshot='prd-' + config['project'])
    report = observe(facts, config)
    if report is not None:
        print(json.dumps(report))
    if config['backend'] == 'sonnet':
        from af_retire_claude import retire
        for ticket in retire(facts, config, ticket_state.all_validations_passed):
            print(ticket, 'terminal Claude context archived and released')
    return 0


def main() -> int:
    config = settings(private_json(Path(os.environ['AF_RUNTIME_CONFIG'])))
    env = runtime_environment(config, dict(os.environ))
    os.environ.update(env)
    sys.path[:0] = [str(Path(config['factory']) / 'hooks'), str(Path(config['factory']) / 'src')]
    args = sys.argv[1:]
    if not args:
        raise ValueError('runtime command required')
    role, args = args[0], args[1:]
    if role == 'node':
        args = prepare_node(args, config, env)
        heavy, kind = command_kind(args, config)
        command = [config['real_node'], *args]
        if heavy and not env.get('AF_VALIDATION_ACTIVE'):
            admit(config, kind, env)
            command = scope(config, command, kind)
    elif role in {'claude', 'codex'}:
        for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
                    'ANTHROPIC_MODEL', 'CLAUDE_CODE_OAUTH_TOKEN', 'OPENAI_API_KEY',
                    'OPENAI_BASE_URL', 'XAI_API_KEY'):
            env.pop(key, None)
        preflight = bool(env.get('AF_RUNTIME_PREFLIGHT'))
        if preflight:
            env.pop('AF_SERVICE_UNIT', None)
        elif len(args) >= 2 and args[-2] == '--':
            import re
            ticket = re.search(r'/af-build\s+' + re.escape(config['project']) + r'\s+([A-Za-z0-9_.-]+)\b', args[-1])
            if ticket:
                with (Path(config['runtime']) / 'state/worker-start-events.jsonl').open('a') as stream:
                    stream.write(json.dumps(dict(at=time.time(), event='worker_launch',
                                                 backend='sonnet' if role == 'claude' else 'codex',
                                                 ticket=ticket[1], pid=os.getpid())) + '\n')
        command = scope(config, [config['real_' + role], *args], owner=not preflight)
    elif role == 'python':
        command = [config['python'], *args]
    elif role == 'maintenance':
        return maintenance(config, env)
    elif role == 'observe':
        import _praxis as praxis
        from af_worker_metrics import observe
        facts = praxis.facts_by(category='requirement', space=config['project'],
                                snapshot='prd-' + config['project'])
        print(json.dumps(observe(facts, config, force=True)))
        return 0
    elif role in {'supervise', 'check'}:
        scripts = Path(config['factory']) / 'scripts'
        driver = ['bash', str(scripts / 'af-ticket-loop.sh')]
        if role == 'check':
            env['AF_RUNTIME_PREFLIGHT'] = '1'
            command = [*driver, '--check']
        else:
            command = ['bash', str(scripts / 'af-supervise.sh'), *driver, config['project'],
                       config['worktree'], str(config['pg_port']), str(config['redis_port'])]
    else:
        raise ValueError(f'unknown runtime command: {role}')
    os.execvpe(command[0], command, env)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(64)
