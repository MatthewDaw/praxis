#!/usr/bin/env python3
"""Keep a Windows-started wsl.exe invocation alive only while its owned job runs."""
import argparse
import os
import re
import subprocess
import time

from af_runtime_config import user_bus_environment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unit', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'praxis-af-[A-Za-z0-9_.-]+\.service', args.unit):
        parser.error('an owned Praxis runtime service is required')
    env = user_bus_environment(dict(os.environ))
    while subprocess.run(['systemctl', '--user', 'is-active', '--quiet', args.unit], env=env).returncode == 0:
        time.sleep(30)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
