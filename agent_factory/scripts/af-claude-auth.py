#!/usr/bin/env python3
"""Copy an existing Claude subscription login into a private runtime config."""
import argparse
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def validate(data: str) -> dict:
    value = json.loads(data)
    oauth = value.get('claudeAiOauth', {})
    if not oauth.get('accessToken') or not oauth.get('refreshToken'):
        raise ValueError('Claude subscription OAuth credentials required, not an MCP-only identity')
    if oauth.get('subscriptionType') not in {'max', 'pro'}:
        raise ValueError('existing Max/Pro subscription login required')
    expiry = oauth.get('expiresAt')
    if not isinstance(expiry, (int, float)) or expiry <= time.time() * 1000:
        raise ValueError('existing Claude credential is expired; refresh that login first')
    return value


def install(data: str, destination: Path) -> None:
    value = validate(data)
    destination.mkdir(parents=True, exist_ok=True)
    destination.chmod(0o700)
    path = destination / '.credentials.json'
    temporary = destination / '.credentials.next'
    fd = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
    temporary.chmod(0o600)
    temporary.replace(path)
    settings = destination / 'settings.json'
    config = json.loads(settings.read_text()) if settings.exists() else {}
    config.setdefault('permissions', {})['defaultMode'] = 'bypassPermissions'
    settings.write_text(json.dumps(config))
    settings.chmod(0o600)
    onboarding = destination / '.claude.json'
    state = json.loads(onboarding.read_text()) if onboarding.exists() else {}
    state.update(hasCompletedOnboarding=True, theme='dark', bypassPermissionsModeAccepted=True,
                 hasResetAutoModeOptInForDefaultOffer=True)
    onboarding.write_text(json.dumps(state))
    onboarding.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--from-keychain', action='store_true')
    source.add_argument('--from-file', type=Path)
    source.add_argument('--stdin', action='store_true', help='read a private transport pipe; never echo it')
    parser.add_argument('--account', default=getpass.getuser())
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    if args.from_keychain:
        data = subprocess.check_output(['security', 'find-generic-password', '-s',
                                        'Claude Code-credentials', '-a', args.account, '-w'], text=True)
    elif args.from_file:
        data = args.from_file.read_text()
    else:
        data = sys.stdin.read()
    install(data, args.destination)
    print('Existing subscription login installed privately; no credential printed.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError):
        print('Could not import a valid existing Claude subscription login; credential contents withheld.', file=sys.stderr)
        raise SystemExit(2)
