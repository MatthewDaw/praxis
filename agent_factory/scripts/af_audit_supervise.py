"""Managed audit turns; Praxis, never process exit, decides phase completion."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from af_audit_prerequisites import identity_plan, directive


TERMINAL = {'complete', 'partial'}


def status(config: dict, env: dict) -> dict:
    result = subprocess.run([
        config['python'], '-m', 'agent_factory.af_audit_fix', '--org', env['PRAXIS_ORG'],
        '--project', config['project'], '--run', config['audit_run'], 'status',
    ], cwd=config['worktree'], env=env, capture_output=True, text=True, timeout=120)
    if result.returncode:
        # Do not print stderr, which may contain an unexpected credential-bearing URL.
        raise RuntimeError('Praxis audit status unavailable; no audit completion inferred')
    value = json.loads(result.stdout)
    if value.get('project') != config['project'] or value.get('run') != config['audit_run']:
        raise ValueError('audit status scope mismatch')
    if value.get('phase') not in {'audit', 'fix', 'deploy', 'reaudit', *TERMINAL}:
        raise ValueError('unknown canonical audit phase')
    return value


def agent_command(config: dict, prompt: Path, output: Path) -> list[str]:
    engine = 'claude' if config['backend'] == 'sonnet' else 'codex'
    return [config['real_node'], str(Path(config['factory']) / 'skills/af-audit-fix/scripts/run-agent.mjs'),
            '--engine', engine, '--cwd', config['worktree'], '--prompt-file', str(prompt),
            '--output-dir', str(output), '--cli-path', str(Path(config['runtime']) / 'bin' / engine),
            '--permissions', 'full-access', '--timeout-ms', str(config['audit_batch_timeout_s'] * 1000)]


def run_turn(config: dict, env: dict, prompt: str, label: str) -> int:
    evidence = Path(config['runtime']) / 'state' / 'audit-agent-evidence'
    evidence.mkdir(mode=0o700, exist_ok=True)
    # A unique attempt folder is a diagnostic/evidence location, never a phase flag.
    attempt = evidence / (label + '-' + uuid.uuid4().hex)
    attempt.mkdir(mode=0o700)
    prompt_file = attempt / 'prompt.md'
    prompt_file.write_text(prompt)
    prompt_file.chmod(0o600)
    result = subprocess.run(agent_command(config, prompt_file, attempt / 'process'),
                            cwd=config['worktree'], env=env, capture_output=True, text=True)
    print(json.dumps({'event': 'audit_agent_turn', 'attempt': str(attempt),
                      'engine': config['backend'], 'process_exit': result.returncode,
                      'audit_completion': 'determined_only_by_praxis'}), flush=True)
    return result.returncode


def preflight(config: dict, env: dict) -> int:
    current = status(config, env)
    if current['phase'] in TERMINAL:
        print(json.dumps({'phase': current['phase'], 'already_terminal': True}), flush=True)
        return 0
    probe = subprocess.run(config['audit_browser_probe'], cwd=config['worktree'], env=env,
                           capture_output=True, text=True, timeout=120)
    if probe.returncode:
        raise RuntimeError('audit browser preflight failed; remote audit not started')
    with tempfile.TemporaryDirectory(prefix='.af-audit-probe-', dir=config['worktree']) as directory:
        root = Path(directory)
        expected = uuid.uuid4().hex
        source, destination = root / 'read.txt', root / 'written.txt'
        source.write_text(expected)
        prompt = (f'Read {source} using a file tool. Write its exact contents to {destination} '
                  'using a file tool. Do nothing else. This is a bounded runtime preflight; '
                  'do not audit the application or modify any other file.')
        turn_env = {**env, 'AF_RUNTIME_PREFLIGHT': '1'}
        result = run_turn(config, turn_env, prompt, 'preflight')
        if result or not destination.is_file() or destination.read_text() != expected:
            raise RuntimeError('audit model read/write preflight failed; remote audit not started')
    print(json.dumps({'event': 'audit_preflight_passed', 'engine': config['backend'],
                      'browser_executed': True, 'model_file_tools_verified': True}), flush=True)
    return 0


def fingerprint(current: dict) -> str:
    return hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest()


def plan_assignment(config: dict, current: dict, env: dict) -> dict:
    # Same explicit runtime environment and org pin as status(); never ambient auth.
    command = [config['python'], str(Path(config['factory']) / 'scripts/af_audit_prerequisites.py')]
    result = subprocess.run(command, input=json.dumps({'config': config, 'current': current,
                            'expected_org': env['PRAXIS_ORG']}), cwd=config['worktree'], env=env,
                            capture_output=True, text=True,
                            timeout=3 * config.get('audit_identity_timeout_s', 120) + 120)
    if result.returncode:
        raise RuntimeError('Scoped Praxis prerequisite handler unavailable; no completion inferred')
    return json.loads(result.stdout)


def supervise(config: dict, env: dict, *, sleep=time.sleep) -> int:
    no_progress = 0
    identity_notice = None
    while True:
        before = status(config, env)
        phase = before['phase']
        print(json.dumps({'event': 'audit_phase_observed', 'phase': phase,
                          'counts': before.get('counts', {})}), flush=True)
        if phase in TERMINAL:
            # partial remains plainly partial; service success only means it stopped
            # correctly, never that every case passed.
            return 0
        if phase in config['audit_wait_phases']:
            sleep(30)
            continue
        if phase not in config['audit_phases']:
            raise ValueError('canonical phase is outside this coordinator assignment')
        assignment = ''
        if phase == 'audit':
            plan = plan_assignment(config, before, env)
            assignment = directive(plan)
            if plan['no_runnable_originals'] and plan['deferred_identity_ids']:
                notice = fingerprint(plan['dependency'])
                if notice != identity_notice:
                    print(json.dumps({'event': 'audit_requires_identity',
                                      'affected_originals': len(plan['deferred_identity_ids']),
                                      'actionable_blocker': plan['dependency']['actionable_blocker'],
                                      'coverage_passed': False}), flush=True)
                    identity_notice = notice
                if plan.get('wait_for_identity'):
                    sleep(30)
                    continue
                return 78
        skill = Path(config['factory']) / 'skills/af-audit-fix/SKILL.md'
        prompt = (f'Execute a bounded batch through the af-audit-fix skill at {skill}.\n'
                  f'Canonical scope: org={env["PRAXIS_ORG"]}, project={config["project"]}, '
                  f'run={config["audit_run"]}, assigned current phase={phase}.\n'
                  'Read canonical Praxis status first. Never work outside this assigned phase. '
                  'A source census, test count or process success is not a browser pass. '
                  'Read the run target and pinned revision from Praxis. Preserve evidence, '
                  'record actual case outcomes in Praxis, and reconcile discovered controls. '
                  'Do not weaken assertions or label blocked cases passed. '
                  'When the phase gates truly hold and all child workers have stopped, '
                  'advance via the canonical audit CLI. Otherwise leave precise progress '
                  'in Praxis and return after this bounded batch.\n\n' +
                  Path(config['audit_prompt_file']).read_text() + assignment)
        result = run_turn(config, env, prompt, phase)
        after = status(config, env)
        if fingerprint(before) == fingerprint(after):
            no_progress += 1
        else:
            no_progress = 0
        if no_progress >= config['audit_no_progress_limit']:
            print(json.dumps({'event': 'audit_requires_attention', 'phase': phase,
                              'reason': 'repeated turns made no canonical progress',
                              'last_process_exit': result, 'coverage_passed': False}), flush=True)
            # The owned unit explicitly prevents restart for this exit. Repeated
            # auth/tool failures must not run an infinite costly retry loop.
            return 78
        if result:
            sleep(30)
