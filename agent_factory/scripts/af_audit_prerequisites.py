"""Prerequisite-aware audit assignment; mutable observations live in Praxis.

Commands are project-defined argument arrays, never shell snippets. Probe stdout
is a small {available: bool, revision?: str} object; all other output stays private.
Provisioners must be idempotent: a crash can occur after their effect but before
its confirmation. A restarted coordinator always probes before retrying.
"""
from __future__ import annotations

import json
import hashlib
import sys
import re
import subprocess
import time
from typing import Callable

IDENTITY_WORDS = re.compile(r'admin|official|coach|participant|guardian|minor|authenticated|adult|delegate|member|reviewer|player|parent|family|user|operator|manager|staff|subscriber|account', re.I)


def requires_identity(scenario: dict) -> bool:
    if isinstance(scenario.get('requires_identity'), bool):
        return scenario['requires_identity']
    roles = scenario.get('roles', [scenario.get('required_role', scenario.get('role', ''))])
    public_roles = {'anonymous', 'public', 'public reader', 'unauthenticated', 'visitor', 'any visitor'}
    return any(str(role).strip().casefold() not in public_roles for role in roles if str(role).strip())


def execute(argv: list[str], env: dict, cwd: str, timeout: int) -> dict:
    """Never expose arbitrary command output in shared logs or the ledger."""
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            return {'ok': False, 'reason': 'command_failed', 'exit_code': result.returncode}
        if len(result.stdout) > 8192:
            return {'ok': False, 'reason': 'invalid_probe_output'}
        try:
            output = json.loads(result.stdout) if result.stdout.strip() else {}
        except (ValueError, TypeError):
            output = {}
        return {'ok': True, 'output': output}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'reason': 'command_timeout'}
    except OSError:
        return {'ok': False, 'reason': 'command_unavailable'}


def identity_plan(config: dict, current: dict, env: dict, save: Callable[[dict], None],
                  *, run=execute, now=time.time) -> dict:
    """Observe one shared identity dependency and choose runnable originals.

    ``save`` merges only identity_prerequisite into the canonical run fact. Never
    marks a case passed or blocked; missing prerequisites are assignment state.
    """
    prior = dict(current.get('run_meta', {}).get('identity_prerequisite', {}))
    state = dict(prior)
    probe = config.get('audit_identity_probe')
    provider = config.get('audit_identity_provisioner')
    clock = now()
    available = False
    probe_error = None
    if probe:
        observation = run(probe, env, config['worktree'], config.get('audit_identity_timeout_s', 120))
        value = observation.get('output', {})
        if observation.get('ok') and isinstance(value, dict) and isinstance(value.get('available'), bool):
            available = value['available']
            # Revision is an opaque fixture generation/hash, never actor material.
            revision = value.get('revision')
            if isinstance(revision, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', revision):
                state['revision'] = revision
        else:
            probe_error = observation.get('reason', 'invalid_probe_output')
    else:
        probe_error = 'identity_probe_not_configured'
    if available:
        if prior.get('status') != 'available' or state.get('revision') != prior.get('revision'):
            state['available_since'] = clock
        state.update(status='available', actionable_blocker=None, probe_error=None)
    else:
        attempts = state.get('attempts', 0)
        due = clock >= state.get('retry_after', 0)
        limit = config.get('audit_identity_max_attempts', 3)
        if provider and probe and attempts < limit and due:
            # Persist BEFORE invoking to bound retries across crashes/restarts.
            state.update(status='provisioning', attempts=attempts + 1,
                         retry_after=clock + config.get('audit_identity_retry_s', 300))
            save({'identity_prerequisite': dict(state)})
            outcome = run(provider, env, config['worktree'], config.get('audit_identity_timeout_s', 120))
            # Success is not proof of provisioned identity. Re-probe every time.
            observation = run(probe, env, config['worktree'], config.get('audit_identity_timeout_s', 120))
            value = observation.get('output', {})
            available = bool(observation.get('ok') and isinstance(value, dict) and value.get('available') is True)
            if available:
                revision = value.get('revision')
                if isinstance(revision, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', revision):
                    state['revision'] = revision
                state['available_since'] = clock
                state.update(status='available', actionable_blocker=None, probe_error=None)
            else:
                state.update(status='provisioning_failed', probe_error=outcome.get('reason', 'provisioner_did_not_make_identity_available'))
        elif state.get('status') != 'provisioning_failed':
            state['status'] = 'awaiting_identity'
        if not available:
            state['actionable_blocker'] = (
                'Provide a project-owned identity probe and idempotent provisioner, or deliver '
                'the dedicated browser fixture handoff then restart/recheck this run. '
                'Browser workers must not import cloud/database credentials. '
                'Blocked originals will be reconsidered after the probe reports availability.')
            state['probe_error'] = probe_error or state.get('probe_error')
    originals = set(current.get('run_meta', {}).get('inventory_ids', []))
    candidates = [c for c in current.get('cases', [])
                  if (not originals or c['case_id'] in originals)
                  and c.get('initial', {}).get('status', 'not_run') in {'not_run', 'blocked'}]
    protected = [c['case_id'] for c in candidates if requires_identity(c.get('scenario', {}))]
    state['affected_original_ids'] = sorted(protected)
    reserved = {key for key, value in current.get('run_meta', {}).get('scheduling_assignments', {}).items()
                if value.get('status') == 'reserved'}
    external = dict(state.get('external_deferred', {}))
    deferred_external = []
    groups = current.get('run_meta', {}).get('audit_prerequisites', {})
    for case in candidates:
        result = case.get('initial', {})
        dependencies = result.get('blocking_prerequisites', [])
        if (result.get('status') != 'blocked' or not isinstance(dependencies, list) or not dependencies
                or 'browser-identity' in dependencies
                or result.get('prerequisite_revision') != (state.get('revision') or 'available')):
            continue
        fingerprint = hashlib.sha256(json.dumps({d: groups.get(d, {}) for d in dependencies}, sort_keys=True).encode()).hexdigest()
        verdict = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
        old = external.get(case['case_id'])
        if old is None or old['result_fingerprint'] != verdict:
            external[case['case_id']] = {'dependency_fingerprint': fingerprint, 'result_fingerprint': verdict}
            deferred_external.append(case['case_id'])
        elif old['dependency_fingerprint'] == fingerprint:
            deferred_external.append(case['case_id'])
    state['external_deferred'] = external
    runnable = [c['case_id'] for c in candidates if c['case_id'] not in reserved
                and c['case_id'] not in deferred_external
                and (available or c['case_id'] not in protected)]
    runnable.sort(key=lambda cid: (next(c for c in candidates if c['case_id'] == cid)['initial'].get('status') != 'blocked', cid))
    if state != prior:
        save({'identity_prerequisite': state})
    return {'identity_available': available, 'dependency': state,
            'runnable_original_ids': runnable, 'deferred_identity_ids': [] if available else sorted(protected),
            'deferred_external_ids': sorted(deferred_external),
            'reserved_ids': sorted(reserved), 'no_runnable_originals': not runnable,
            'wait_for_identity': bool(not available and probe and (not provider or state.get('attempts', 0) < config.get('audit_identity_max_attempts', 3)))}


def directive(plan: dict) -> str:
    brief = {k: plan[k] for k in ('identity_available', 'progress', 'next_batches') if k in plan}
    if 'next_batches' not in brief:
        brief['runnable_original_ids'] = plan.get('runnable_original_ids', [])[:12]
    if plan.get('identity_available') is False:
        brief['actionable_blocker'] = plan['dependency'].get('actionable_blocker')
    return ('\nPrerequisite handler assignment (canonical, overrides anonymous-first probing):\n'
            + json.dumps(brief, sort_keys=True) + '\n'
            'Execute a multi-page sweep from next_batches using prepared profiles. Batch known actions and '
            'assertions in one script. Finish close_first and compatible remaining checks in this visit, including reload. '
            'Reuse verified_checks only for the same concrete scope, behavior and applicable role; directly exercise permission differences. '
            'Emit structured checkpoints via record-sweep once per sweep. Check ordinary interactions on each reachable page before '
            'deepening a single workflow. Save verified results and queue any remaining required '
            'steps with evidence. A slow or complex action should not hold the other pages. '
            'Reconsider blocked originals when '
            'identity_available=true; verify real authentication and execute the missing behavior. '
            'Do not revisit deferred protected routes anonymously or add public-control variants '
            'to work around a shared missing prerequisite. One missing dependency does not prove '
            'any case failed or passed. Respect reserved IDs. Continue independent runnable work. '
            'If no runnable originals remain, reconcile and evaluate genuine freeze/partial gates; '
            'do not invent variants or relaunch unchanged external blockers.\n')


def main() -> int:
    try:
        from agent_factory.af_audit_fix import Audit
        from hooks import _praxis
        import os
        payload = json.load(sys.stdin)
        config = payload['config']
        _praxis.assert_org_matches(payload['expected_org'])
        ledger = Audit(config['project'], config['audit_run'])
        plan = identity_plan(config, payload['current'], dict(os.environ),
                             lambda data: ledger.patch(ledger.one('run'), data))
        from agent_factory.af_audit_queue import audit_queue, next_visits
        current = payload['current']
        meta = current['run_meta']
        deferrals = {'identity': plan['deferred_identity_ids'], 'external': plan['deferred_external_ids']}
        if deferrals != meta.get('queue_deferrals'):
            ledger.patch(ledger.one('run'), {'queue_deferrals': deferrals})
        queue = audit_queue({**current, 'run_meta': {**meta, 'queue_deferrals': deferrals}})
        if queue['state'] == 'mapped':
            plan['progress'] = queue['progress']
            plan['next_batches'] = next_visits(current, queue)
        print(json.dumps(plan))
        return 0
    except Exception:
        print(json.dumps({'error': 'audit prerequisite handler failed; inspect private operator diagnostics'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
