#!/usr/bin/env python3
"""Reap scopes explicitly bound to this failed driver's service before retrying."""
import os
import subprocess
import sys


def main():
    owner = os.environ.get('AF_SERVICE_UNIT')
    if not owner:
        return 0
    result = subprocess.run(['systemctl', '--user', 'list-units', '--type=scope',
                             '--plain', '--no-legend', '--no-pager'], capture_output=True, text=True, check=True)
    for line in result.stdout.splitlines():
        unit = line.split()[0]
        bound = subprocess.run(['systemctl', '--user', 'show', unit, '--property=BindsTo', '--value'],
                               capture_output=True, text=True, check=True).stdout.split()
        if owner in bound:
            subprocess.run(['systemctl', '--user', 'stop', unit], check=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
