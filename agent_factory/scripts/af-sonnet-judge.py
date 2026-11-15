#!/usr/bin/env python3
"""Run a fresh, tool-free Sonnet judge on stdin using existing CLI authentication."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


def judge_command(binary, model='sonnet'):
    # MCP configuration is variadic; the delimiter keeps the prompt out of it.
    return [binary, '-p', '--model', model, '--no-session-persistence', '--tools', '',
            '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir')
    parser.add_argument('--model', default='sonnet')
    args = parser.parse_args()
    prompt = sys.stdin.read()
    if not prompt.strip():
        parser.error('an external judge prompt is required on stdin')
    binary = shutil.which('claude') or str(Path.home() / '.local/bin/claude')
    env = dict(os.environ)
    if args.config_dir:
        env['CLAUDE_CONFIG_DIR'] = args.config_dir
    # Existing Claude CLI login supplies authentication; avoid alternate providers.
    for name in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
                 'CLAUDE_CODE_OAUTH_TOKEN'):
        env.pop(name, None)
    result = subprocess.run(judge_command(binary, args.model), input=prompt, text=True, env=env)
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
