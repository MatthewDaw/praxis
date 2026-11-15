"""Keep a per-job Windows WSL invocation alive without changing global WSL settings."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess


def powershell_command(config: dict, distribution: str) -> str:
    helper = str(Path(config['factory']) / 'scripts/af-wsl-keepalive.py')
    arguments = subprocess.list2cmdline(['--distribution', distribution, '--exec',
                                        config['python'], helper, '--unit', config['service']])
    quote = lambda value: "'" + value.replace("'", "''") + "'"
    return (
        "$d=Join-Path $env:LOCALAPPDATA 'Praxis';"
        "New-Item -ItemType Directory -Force -Path $d | Out-Null;"
        "$p=Start-Process -FilePath 'wsl.exe' -WindowStyle Hidden -PassThru "
        f"-ArgumentList {quote(arguments)} "
        f"-RedirectStandardOutput (Join-Path $d {quote(config['project'] + '-keepalive.out')}) "
        f"-RedirectStandardError (Join-Path $d {quote(config['project'] + '-keepalive.err')});"
        "$p.Id"
    )


def start(config: dict) -> int | None:
    if not os.environ.get('WSL_DISTRO_NAME'):
        return None
    binary = shutil.which('powershell.exe')
    if not binary:
        raise RuntimeError('WSL host keepalive needs Windows PowerShell interop')
    script = powershell_command(config, os.environ['WSL_DISTRO_NAME'])
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    result = subprocess.run([binary, '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                            capture_output=True, text=True, check=True, timeout=30)
    pid = int(result.stdout.strip())
    path = Path(config['runtime']) / 'state/wsl-host-keepalive.json'
    path.write_text(json.dumps(dict(windows_pid=pid, service=config['service'])))
    path.chmod(0o600)
    return pid
