#!/usr/bin/env python3
"""Launch an owned resource scope; its lifetime follows the build service."""
import argparse
import os
import subprocess


def scope_command(unit, slice_name, command, owner=None, memory_high=None, memory_max=None):
    if not command:
        raise ValueError('a command is required')
    args = ['systemd-run', '--user', '--scope', '--quiet', '--collect',
            '--slice=' + slice_name, '--unit=' + unit]
    if owner:
        # Do not start a scheduler as a side effect of an orphan/probe launching.
        result = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', owner])
        if result.returncode:
            raise RuntimeError('owning build service is inactive: ' + owner)
        args += ['--property=BindsTo=' + owner, '--property=After=' + owner]
    if memory_high:
        args += ['--property=MemoryHigh=' + memory_high]
    if memory_max:
        args += ['--property=MemoryMax=' + memory_max]
    return args + ['--', *command]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unit', required=True)
    parser.add_argument('--slice', required=True, dest='slice_name')
    parser.add_argument('--owner-unit', default=os.environ.get('AF_SERVICE_UNIT'))
    parser.add_argument('--memory-high')
    parser.add_argument('--memory-max')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = vars(parser.parse_args())
    args['owner'] = args.pop('owner_unit')
    if args['command'][:1] == ['--']:
        args['command'] = args['command'][1:]
    try:
        command = scope_command(**args)
    except (RuntimeError, ValueError) as error:
        parser.exit(125, str(error) + '\n')
    os.execvp(command[0], command)


if __name__ == '__main__':
    main()
