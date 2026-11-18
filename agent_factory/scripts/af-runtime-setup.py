#!/usr/bin/env python3
"""Install a reproducible, isolated factory runtime; optionally start its owned service."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import getpass
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

from af_runtime_config import private_json, runtime_environment, settings, user_bus_environment


def real_tool(name: str) -> str:
    """Do not accidentally install a wrapper that calls another managed wrapper."""
    candidates = [shutil.which(name), str(Path.home() / '.local/bin' / name),
                  '/usr/bin/' + name, '/usr/local/bin/' + name, '/opt/homebrew/bin/' + name]
    for value in candidates:
        if not value:
            continue
        path = Path(value)
        if not path.is_file() or not os.access(path, os.X_OK):
            continue
        with path.open('rb') as stream:
            prefix = stream.read(8192)
        if b'AF_RUNTIME_CONFIG' in prefix or b'MVPVUE_HEAVY_COMMAND_ACTIVE' in prefix:
            continue
        return str(path.absolute())
    return str(Path.home() / '.local/bin' / name)  # backend preflight will report a missing CLI


def host_defaults() -> dict:
    path = Path('/proc/meminfo')
    if not path.exists():
        return {}
    total = next(int(line.split()[1]) // (1024 * 1024) for line in path.read_text().splitlines()
                 if line.startswith('MemTotal:'))
    maximum = min(12, max(4, total // 2))
    return dict(memory_max_gib=maximum, memory_high_gib=max(2, maximum * 2 // 3),
                unit_memory_gib=max(1, maximum // 4), build_memory_gib=max(2, maximum // 2),
                host_available_gib=max(1, min(8, total // 3)))


@contextmanager
def project_lock(worktree: Path, enabled: bool):
    """Use the driver's own mutex while changing its pinned runtime settings."""
    if not enabled:
        yield
        return
    lock = worktree / '.af-loop.lock'
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        identity = lock.stat().st_ino
        try:
            pid = int(lock.read_text().split()[0])
            os.kill(pid, 0)
        except ProcessLookupError:
            if lock.exists() and lock.stat().st_ino == identity:
                lock.unlink()
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except (ValueError, IndexError, PermissionError):
            raise ValueError('worktree has an unproven driver lock; keep it and inspect its owner') from None
        else:
            raise ValueError('worktree has a driver lock; keep the existing run and use a separate worktree') from None
    identity = os.fstat(fd).st_ino
    with os.fdopen(fd, 'w') as stream:
        stream.write(f'{os.getpid()} setup\n')
    try:
        yield
    finally:
        if lock.exists() and lock.stat().st_ino == identity:
            lock.unlink()


def private_write(path: Path, data: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, 'w') as stream:
        stream.write(data)
    temporary.chmod(mode)
    temporary.replace(path)


def source_files(source: Path) -> dict[str, tuple[bytes, int]]:
    """Use committed source when available, never silently deploy a dirty driver."""
    git = subprocess.run(['git', '-C', str(source), 'rev-parse', '--show-toplevel'],
                         capture_output=True, text=True)
    files = {}
    if git.returncode == 0:
        root = Path(git.stdout.strip())
        relative = source.relative_to(root).as_posix()
        revision = 'HEAD:' + relative if relative != '.' else 'HEAD'
        data = subprocess.check_output(['git', '-C', str(root), 'archive', revision])
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            for member in archive.getmembers():
                name = Path(member.name)
                if not member.isfile() or name.parts[0] in {'tests', 'evals', 'state'}:
                    continue
                if name.is_absolute() or '..' in name.parts or name.name == '.env':
                    continue
                files[name.as_posix()] = (archive.extractfile(member).read(), member.mode)
    else:
        for path in source.rglob('*'):
            relative = path.relative_to(source)
            if any(x in {'.git', '.venv', '__pycache__', 'tests', 'evals', 'state'}
                   or x.startswith('.env') or x == '.credentials.json' for x in relative.parts):
                continue
            if path.is_file() and not path.is_symlink():
                files[relative.as_posix()] = (path.read_bytes(), path.stat().st_mode)
    if 'scripts/af_runtime_entry.py' not in files:
        raise ValueError('factory release lacks managed runtime; commit the setup before installing')
    return files


def snapshot(source: Path, runtime: Path) -> Path:
    files = source_files(source)
    digest = hashlib.sha256()
    for name, (data, mode) in sorted(files.items()):
        digest.update(name.encode() + b'\0' + bytes([bool(mode & 0o111)]) + data)
    destination = runtime / 'factory' / digest.hexdigest()[:20]
    if destination.exists():
        for name, (data, _) in files.items():
            if (destination / name).read_bytes() != data:
                raise ValueError('immutable factory snapshot was modified: ' + str(destination))
        return destination
    destination.mkdir(parents=True)
    for name, (data, mode) in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o555 if mode & 0o111 else 0o444)
    for directory in sorted((p for p in destination.rglob('*') if p.is_dir()), reverse=True):
        directory.chmod(0o555)
    destination.chmod(0o555)
    return destination


def systemd_quote(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def units(config: dict) -> dict[str, str]:
    runtime = Path(config['runtime'])
    name = config['service'][:-8]
    command = systemd_quote(str(runtime / 'bin/af-runtime'))
    return {
        config['slice']: f'''[Unit]
Description=Praxis resource budget for {config['project']}
[Slice]
MemoryAccounting=yes
MemoryHigh={config['memory_high_gib']}G
MemoryMax={config['memory_max_gib']}G
MemorySwapMax=1G
''',
        config['service']: f'''[Unit]
Description=Praxis unattended build for {config['project']}
Wants={name}-maintenance.timer
[Service]
Type=simple
WorkingDirectory={config['worktree'].replace('%', '%%')}
ExecStart={command} supervise
Slice={config['slice']}
UMask=0077
KillMode=control-group
TimeoutStopSec=90
Restart=on-failure
RestartSec=30
{('RestartPreventExitStatus=78' if config.get('mode', 'build') == 'audit' else '')}
[Install]
WantedBy=default.target
''',
        name + '-maintenance.service': f'''[Unit]
Description=Continue unfinished idle Praxis conversations
Requisite={config['service']}
After={config['service']}
PartOf={config['service']}
[Service]
Type=oneshot
ExecStart={command} maintenance
Slice={config['slice']}
UMask=0077
MemoryHigh=128M
MemoryMax=256M
''',
        name + '-maintenance.timer': f'''[Unit]
Description=Maintain owned Praxis workers
PartOf={config['service']}
[Timer]
OnActiveSec=60
OnUnitActiveSec=60
AccuracySec=5
Unit={name}-maintenance.service
''',
    }


def install(values: dict, source: Path, *, unit_dir: Path | None = None,
            pin_project: bool = True) -> dict:
    with project_lock(Path(values['worktree']).resolve(), pin_project):
        return install_locked(values, source, unit_dir=unit_dir, pin_project=pin_project)


def install_locked(values: dict, source: Path, *, unit_dir: Path | None,
                   pin_project: bool) -> dict:
    runtime = Path(values['runtime']).resolve()
    existing = runtime / 'config.json'
    if existing.exists():
        old = private_json(existing)
        live = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', old['service']],
                              capture_output=True, env=user_bus_environment(dict(os.environ))) if shutil.which('systemctl') else None
        if live is not None and live.returncode == 0:
            raise ValueError('live runtime retained; stop it explicitly before reconfiguring')
    runtime.mkdir(parents=True, exist_ok=True)
    runtime.chmod(0o700)
    for name in ('state', 'bin', 'units'):
        (runtime / name).mkdir(exist_ok=True)
        (runtime / name).chmod(0o700)
    values['runtime'] = str(runtime)
    values['factory'] = str(snapshot(source.resolve(), runtime))
    worktree = Path(values['worktree']).resolve()
    values['worktree'] = str(worktree)
    project_settings = worktree / '.claude/settings.local.json'
    original = json.loads(project_settings.read_text()) if project_settings.exists() else {}
    if not values.get('environment_file'):
        environment = original.get('env', {})
        if not environment.get('PRAXIS_API_KEY') or not environment.get('PRAXIS_ORG'):
            raise ValueError('project needs its existing Praxis key and org, or --environment-file')
        private_write(runtime / 'environment.json', json.dumps(environment))
        values['environment_file'] = str(runtime / 'environment.json')
    identity = private_json(Path(values['environment_file']))
    if not identity.get('PRAXIS_API_KEY') or not identity.get('PRAXIS_ORG'):
        raise ValueError('private environment needs this project\'s existing Praxis key and org')
    config = settings(values)
    if config['mode'] == 'audit':
        prompt = Path(config['audit_prompt_file']).read_text()
        if not prompt.strip():
            raise ValueError('audit prompt must not be empty')
        prompt_path = runtime / 'audit-assignment.md'
        private_write(prompt_path, prompt)
        config['audit_prompt_file'] = str(prompt_path)
    if config['application_env_file'] and not config['fixture_defaults_file']:
        config['fixture_defaults_file'] = config['application_env_file']
        if not config['fixture_default_keys']:
            defaults = private_json(Path(config['application_env_file']))
            config['fixture_default_keys'] = [key for key in defaults if key.endswith(
                ('_ALLOWED_ORIGINS', '_API_PROXY_TARGET'))]
    mcp = Path(config.get('mcp_config', worktree / '.mcp.json'))
    mcp_data = json.loads(mcp.read_text()) if mcp.exists() else {'mcpServers': {}}
    private_write(runtime / 'mcp.json', json.dumps(mcp_data))
    config['mcp_config'] = str(runtime / 'mcp.json')
    private_write(existing, json.dumps(config, indent=2))
    environment = runtime_environment(config, dict(os.environ))
    entry = Path(config['factory']) / 'scripts/af_runtime_entry.py'
    launchers = [('af-runtime', None), ('node', 'node'), ('claude', 'claude'),
                 ('codex', 'codex'), ('factory-python', 'python')]
    if config['mode'] == 'audit':
        launchers.append(('python', 'python'))
    for name, role in launchers:
        prefix = f'#!{config["python"]}\nimport os,sys\n'
        body = f'os.environ["AF_RUNTIME_CONFIG"]={str(existing)!r}\n'
        argv = '[sys.argv[0], *sys.argv[1:]]' if role is None else f'[sys.argv[0], {role!r}, *sys.argv[1:]]'
        body += f'os.execv({config["python"]!r}, [{config["python"]!r}, {str(entry)!r}, *{argv}[1:]])\n'
        private_write(runtime / 'bin' / name, prefix + body, 0o700)
    rendered = units(config)
    for name, text in rendered.items():
        private_write(runtime / 'units' / name, text)
        if unit_dir is not None:
            private_write(unit_dir / name, text)
    if pin_project:
        names = {'FACTORY_PROJECT', 'AF_PLUGIN_DIR', 'AF_STATE_DIR', 'AF_SERVICE_UNIT',
                 'AF_SESSION_PREFIX', 'AF_PYTHON', 'PRAXIS_HOOK_PYTHON', 'AF_MODEL_BACKEND',
                 'AF_CLAUDE_MODEL', 'AF_BATCH_MAX', 'AF_ROLLING', 'AF_FAST_VERIFY',
                 'AF_DEPENDENCIES_ADVISORY', 'AF_LEAN_WORKERS', 'AF_TICKET_DEADLINE_ACTION',
                 'AF_RUNTIME_CONFIG', 'AF_OWNER_EXECUTION_POLICY', 'PYTHONPATH', 'PATH',
                 'CLAUDE_CONFIG_DIR', 'AF_RUNTIME_MCP_CONFIG'}
        original.setdefault('env', {}).update({key: environment[key] for key in names if key in environment})
        private_write(project_settings, json.dumps(original, indent=2))
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--worktree', type=Path, required=True)
    parser.add_argument('--profile', type=Path, help='static JSON scope/resource overrides; no credentials')
    parser.add_argument('--environment-file', type=Path)
    parser.add_argument('--application-env-file', type=Path)
    parser.add_argument('--claude-config-dir', type=Path)
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--pg-port', type=int)
    parser.add_argument('--redis-port')
    parser.add_argument('--factory-source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--render-only', action='store_true', help='write private artifacts without pinning the project or installing units')
    parser.add_argument('--start', action='store_true')
    args = parser.parse_args()
    if args.render_only and args.start:
        parser.error('--render-only cannot start a service')
    values = {**host_defaults(), **(json.loads(args.profile.read_text()) if args.profile else {})}
    runtime_name = (args.project + '-audit-' + str(values.get('audit_run', ''))
                    if values.get('mode') == 'audit' else args.project)
    values.update(project=args.project, worktree=str(args.worktree.resolve()), python=str(Path(args.python).absolute()),
                  runtime=str((args.runtime or Path.home() / '.praxis/runtimes' / runtime_name).resolve()))
    for key in ('environment_file', 'application_env_file', 'claude_config_dir', 'pg_port', 'redis_port'):
        value = getattr(args, key)
        if value is not None:
            values[key] = str(value.resolve()) if isinstance(value, Path) else value
    shared_login = Path.home() / '.praxis/auth/claude'
    default_login = shared_login if (shared_login / '.credentials.json').exists() else Path.home() / '.claude'
    values.setdefault('claude_config_dir', os.environ.get('CLAUDE_CONFIG_DIR', str(default_login)))
    for tool in ('node', 'claude', 'codex'):
        values.setdefault('real_' + tool, real_tool(tool))
    if not args.render_only and sys.platform != 'linux':
        parser.error('managed execution needs Linux systemd/cgroup v2; --render-only works anywhere')
    config = install(values, args.factory_source,
                     unit_dir=None if args.render_only else Path.home() / '.config/systemd/user',
                     pin_project=not args.render_only)
    if not args.render_only:
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True,
                       env=user_bus_environment(dict(os.environ)))
    if args.start:
        subprocess.run(['loginctl', '--no-ask-password', 'enable-linger', getpass.getuser()], check=True)
        subprocess.run([str(Path(config['runtime']) / 'bin/af-runtime'), 'check'],
                       cwd=config['worktree'], check=True)
        subprocess.run(['systemctl', '--user', 'enable', '--now', config['service']], check=True,
                       env=user_bus_environment(dict(os.environ)))
        from af_wsl_host import start
        start(config)
    print(json.dumps({key: config[key] for key in ('project', 'runtime', 'factory', 'service', 'backend', 'model')}))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
