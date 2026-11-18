"""Meaningful scheduling/provisioning behavior without any live provider or identity."""
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from af_audit_prerequisites import identity_plan


def current():
    return {'run_meta': {'inventory_ids': ['AUTH', 'TEAMS', 'PUBLIC'], 'scheduling_assignments': {}},
            'cases': [{'case_id': 'AUTH', 'scenario': {'roles': ['organization admin']}, 'initial': {'status': 'blocked'}},
                      {'case_id': 'TEAMS', 'scenario': {'role': 'coach'}, 'initial': {'status': 'not_run'}},
                      {'case_id': 'PUBLIC', 'scenario': {'roles': ['anonymous']}, 'initial': {'status': 'not_run'}}]}


def test_no_provider_groups_shared_identity_once_and_independent_work_proceeds():
    saved = []
    state = current()
    cfg = {'worktree': '/tmp'}
    first = identity_plan(cfg, state, {}, saved.append, now=lambda: 10)
    assert first['deferred_identity_ids'] == ['AUTH', 'TEAMS']
    assert first['runnable_original_ids'] == ['PUBLIC']
    assert len(saved) == 1
    state['run_meta'].update(saved[-1])
    again = identity_plan(cfg, state, {}, saved.append, now=lambda: 11)
    assert again == first
    assert len(saved) == 1  # no recurring setup writes or phantom case verdicts
    assert state['cases'][0]['initial']['status'] == 'blocked'


def test_probe_available_retries_old_blocked_originals_first():
    saved = []
    plan = identity_plan({'worktree': '/tmp', 'audit_identity_probe': ['/probe']}, current(), {}, saved.append,
                         run=lambda *a: {'ok': True, 'output': {'available': True, 'revision': 'fixture2'}}, now=lambda: 20)
    assert plan['runnable_original_ids'] == ['AUTH', 'PUBLIC', 'TEAMS']
    assert not plan['deferred_identity_ids']
    assert saved[-1]['identity_prerequisite']['revision'] == 'fixture2'


def test_provider_success_requires_actual_reprobe_and_persists_before_effect():
    saved, calls = [], []
    def run(argv, *args):
        calls.append(argv[0])
        if argv[0] == '/provide':
            assert saved[-1]['identity_prerequisite']['status'] == 'provisioning'
            return {'ok': True, 'output': {}}
        return {'ok': True, 'output': {'available': calls.count('/probe') > 1, 'revision': 'fixture2'}}
    plan = identity_plan({'worktree': '/tmp', 'audit_identity_probe': ['/probe'],
                          'audit_identity_provisioner': ['/provide']}, current(), {}, saved.append, run=run, now=lambda: 100)
    assert calls == ['/probe', '/provide', '/probe']
    assert plan['identity_available']
    assert saved[-1]['identity_prerequisite']['attempts'] == 1


def test_failed_provider_cooldown_restart_and_limit_allow_independent_work():
    cfg = {'worktree': '/tmp', 'audit_identity_probe': ['/probe'], 'audit_identity_provisioner': ['/provide'],
           'audit_identity_max_attempts': 1}
    state, saved, calls = current(), [], []
    def run(argv, *args):
        calls.append(argv[0])
        return {'ok': False, 'reason': 'command_failed', 'exit_code': 1} if argv[0] == '/provide' else {'ok': True, 'output': {'available': False}}
    plan = identity_plan(cfg, state, {}, saved.append, run=run, now=lambda: 10)
    assert plan['dependency']['status'] == 'provisioning_failed'
    assert plan['runnable_original_ids'] == ['PUBLIC']
    state['run_meta'].update(saved[-1])
    identity_plan(cfg, state, {}, saved.append, run=run, now=lambda: 1000)
    assert calls.count('/provide') == 1
    assert calls.count('/probe') == 3


def test_restart_probes_before_retry_and_does_not_duplicate_completed_provision():
    cfg = {'worktree': '/tmp', 'audit_identity_probe': ['/probe'], 'audit_identity_provisioner': ['/provide']}
    state = current()
    state['run_meta']['identity_prerequisite'] = {'status': 'provisioning', 'attempts': 1, 'retry_after': 500}
    saved, calls = [], []
    def run(argv, *args):
        calls.append(argv[0]); return {'ok': True, 'output': {'available': True, 'revision': 'recovered'}}
    plan = identity_plan(cfg, state, {}, saved.append, run=run, now=lambda: 20)
    assert plan['identity_available']
    assert calls == ['/probe']
    assert plan['dependency']['attempts'] == 1


def test_missing_probe_never_invokes_provider_and_respects_reservations():
    state = current()
    state['run_meta']['scheduling_assignments']['PUBLIC'] = {'status': 'reserved', 'owner': 'other'}
    plan = identity_plan({'worktree': '/tmp', 'audit_identity_provisioner': ['/provide']}, state, {}, lambda x: None,
                         run=lambda *a: pytest.fail('provider cannot run without verification probe'))
    assert plan['no_runnable_originals']
    assert plan['reserved_ids'] == ['PUBLIC']


def test_unknown_grouped_roles_are_not_treated_as_anonymous():
    from af_audit_prerequisites import requires_identity
    assert requires_identity({'roles': ['all roles']})
    assert not requires_identity({'roles': ['anonymous', 'public reader']})
    assert not requires_identity({'requires_identity': False, 'roles': ['all roles']})


def test_unauthenticated_alias_is_independent_despite_matching_word():
    from af_audit_prerequisites import requires_identity
    assert not requires_identity({'roles': ['unauthenticated']})


def test_fresh_external_blocker_is_not_churned_until_its_dependency_changes():
    state, saved = current(), []
    state['cases'][0]['initial'].update(prerequisite_revision='fixture2', blocking_prerequisites=['controlled-inbox'])
    state['run_meta']['audit_prerequisites'] = {'controlled-inbox': {'status': 'missing', 'revision': 'inbox1'}}
    cfg = {'worktree': '/tmp', 'audit_identity_probe': ['/probe']}
    probe = lambda *a: {'ok': True, 'output': {'available': True, 'revision': 'fixture2'}}
    first = identity_plan(cfg, state, {}, saved.append, run=probe)
    assert 'AUTH' not in first['runnable_original_ids']
    assert first['deferred_external_ids'] == ['AUTH']
    state['run_meta'].update(saved[-1])
    second = identity_plan(cfg, state, {}, saved.append, run=probe)
    assert 'AUTH' not in second['runnable_original_ids']
    state['run_meta']['audit_prerequisites']['controlled-inbox'] = {'status': 'available', 'revision': 'inbox2'}
    third = identity_plan(cfg, state, {}, saved.append, run=probe)
    assert third['runnable_original_ids'][0] == 'AUTH'


def test_provider_backoff_keeps_probe_waiting_instead_of_stranding_retry():
    state = current()
    state['run_meta']['identity_prerequisite'] = {'status': 'provisioning_failed', 'attempts': 1, 'retry_after': 500}
    calls = []
    def probe(argv, *args):
        calls.append(argv[0]); return {'ok': True, 'output': {'available': False}}
    plan = identity_plan({'worktree': '/tmp', 'audit_identity_probe': ['/probe'],
                          'audit_identity_provisioner': ['/provide']}, state, {}, lambda _: None, run=probe, now=lambda: 100)
    assert plan['wait_for_identity']
    assert calls == ['/probe']


def test_smoke_fixture_recovery_reprobes_and_requeues_once():
    from af_audit_fixture_recovery import recover_fixtures

    state = current()
    state["smoke"] = {"AUTH": {"result": {"status": "blocked"}}}
    config = {"worktree": "/tmp", "audit_smoke_fixture_providers": {
        "owned": {"case_ids": ["AUTH"], "probe": ["/probe"], "provision": ["/provision"], "handoff": "Use owned fixture."},
    }}
    calls = []
    ready = False

    def run(argv, *_):
        nonlocal ready
        calls.append(argv[0])
        if argv == ["/provision"]:
            ready = True
        return {"ok": True, "output": {"available": ready, "revision": "generation-1"}}

    def save(changes):
        state["run_meta"].update(copy.deepcopy(changes))

    retries, waiting = recover_fixtures(config, state, {}, save, run=run, now=lambda: 1)
    assert not waiting
    assert calls == ["/probe", "/provision", "/probe"]
    assert "generation-1" in retries["AUTH"]
    state["smoke"]["AUTH"]["retry"] = retries["AUTH"]
    assert recover_fixtures(config, state, {}, save, run=run, now=lambda: 400) == (retries, False)
    assert calls.count("/provision") == 1


def test_runtime_recovery_only_requeues_matching_blockers_in_scope():
    from af_audit_fixture_recovery import recover_fixtures

    state = current()
    state["smoke"] = {
        "AUTH": {"result": {"status": "blocked", "blocker": "Local API interrupted"}},
        "TEAMS": {"result": {"status": "blocked", "blocker": "Missing team"}},
        "OUTSIDE": {"result": {"status": "blocked", "blocker": "Local API interrupted"}},
    }
    cfg = {"worktree": "/tmp", "audit_smoke_fixture_providers": {
        "runtime": {"blocker_prefixes": ["Local API interrupted"], "probe": ["/probe"],
                    "provision": ["/provision"], "handoff": "API healthy."},
    }}
    def observe(_, *_args):
        return {"ok": True, "output": {"available": True, "revision": "healthy-1"}}

    retries, waiting = recover_fixtures(cfg, state, {}, lambda _: None, run=observe)
    assert set(retries) == {"AUTH"}
    assert not waiting


def test_failed_fixture_provision_waits_only_until_attempts_are_exhausted():
    from af_audit_fixture_recovery import recover_fixtures

    state = current()
    state["smoke"] = {"AUTH": {"result": {"status": "blocked"}}}
    config = {"worktree": "/tmp", "audit_smoke_fixture_providers": {
        "owned": {"case_ids": ["AUTH"], "probe": ["/probe"], "provision": ["/provision"],
                  "handoff": "Use owned fixture."},
    }}
    calls = []

    def run(argv, *_):
        calls.append(argv[0])
        return {"ok": False}

    def save(changes):
        state["run_meta"].update(copy.deepcopy(changes))

    for clock, waiting in [(1, True), (100, True), (301, True), (601, False), (700, False)]:
        assert recover_fixtures(config, state, {}, save, run=run, now=lambda: clock) == ({}, waiting)
    assert calls == ["/probe", "/provision", "/probe"] * 3


@pytest.mark.parametrize("interruption", [
    {"blocker": "Local runtime interrupted: Chromium crashed"},
    {"blocker": "Local runtime interrupted: scheduling route failed to mount",
     "network": [{"errorText": "net::ERR_INSUFFICIENT_RESOURCES"}]},
])
def test_local_browser_interruption_retries_only_after_fresh_browser_probe(interruption):
    from af_audit_fixture_recovery import offer_local_recovery, recover_fixtures

    state = current()
    state["run_meta"]["target"] = "http://127.0.0.1:15184"
    state["smoke"] = {"AUTH": {"result": {"status": "blocked", **interruption}}}
    config = {
        "worktree": "/tmp", "audit_smoke_local_recovery": True, "audit_browser_probe": ["/browser-probe"],
        "audit_smoke_fixture_providers": {
            "runtime": {"blocker_prefixes": ["Local runtime interrupted:"], "probe": ["/api-probe"],
                        "provision": ["/api-start"], "handoff": "API ready."},
        },
    }
    calls = []
    ready = False

    def run(argv, *_):
        calls.append(argv)
        return {"ok": ready}

    def save(changes):
        state["run_meta"].update(copy.deepcopy(changes))

    assert recover_fixtures(config, state, {}, save, run=run, now=lambda: 1) == ({}, True)
    assert offer_local_recovery(config, state, {}, set(), save) == {}
    assert recover_fixtures(config, state, {}, save, run=run, now=lambda: 90) == ({}, True)
    ready = True
    retries, waiting = recover_fixtures(config, state, {}, save, run=run, now=lambda: 301)
    assert not waiting
    assert "Fresh Chromium preflight passed" in retries["AUTH"]
    assert calls == [["/browser-probe"], ["/browser-probe"]]
    state["smoke"]["AUTH"]["prior_attempts"] = [{"retry": retries["AUTH"]}]
    assert recover_fixtures(config, state, {}, save, run=run, now=lambda: 601) == (retries, False)
    assert len(calls) == 2


def test_recurrent_browser_interruption_can_use_verified_local_fallback():
    from af_audit_fixture_recovery import BROWSER_REVISION, recover_fixtures

    state = current()
    state["run_meta"]["target"] = "http://127.0.0.1:15184"
    record = {"result": {"status": "blocked", "blocker": "Local browser interrupted: module load failed"}}
    state["smoke"] = {"AUTH": record}
    config = {
        "worktree": "/tmp",
        "audit_smoke_local_recovery": True,
        "audit_browser_probe": ["/browser-probe"],
        "audit_browser_fallback": {"origin": "http://127.0.0.1:58803", "probe": ["/preview-probe"]},
    }
    calls = []

    def run(argv, *_):
        calls.append(argv)
        return {"ok": argv == ["/browser-probe"] or len(calls) >= 3}

    def save(changes):
        state["run_meta"].update(copy.deepcopy(changes))

    retries, waiting = recover_fixtures(config, state, {}, save, run=run, now=lambda: 1)
    assert not waiting
    assert BROWSER_REVISION in retries["AUTH"]
    record["retry"] = retries["AUTH"]
    assert recover_fixtures(config, state, {}, save, run=run, now=lambda: 2) == (retries, True)
    retries, waiting = recover_fixtures(config, state, {}, save, run=run, now=lambda: 302)
    assert not waiting
    assert "http://127.0.0.1:58803" in retries["AUTH"]
    record.update(retry=retries["AUTH"], prior_attempts=[{"retry": BROWSER_REVISION}])
    assert recover_fixtures(config, state, {}, save, run=run, now=lambda: 602) == (retries, False)
    assert calls == [["/browser-probe"], ["/preview-probe"], ["/preview-probe"]]


def test_local_smoke_recovery_offers_one_attempt_alongside_pending_work():
    from af_audit_fixture_recovery import SELF_RECOVERY, offer_local_recovery

    state = current()
    state["run_meta"]["target"] = "http://127.0.0.1:15184"
    state["smoke"] = {
        "AUTH": {"result": {"status": "blocked", "blocker": "Missing local identity",
                            "network": ["net::ERR_INSUFFICIENT_RESOURCES"]}},
        "TEAMS": {"result": {"status": "blocked", "blocker": "Missing local team"}},
    }
    config = {"audit_smoke_local_recovery": True, "audit_browser_probe": ["/browser-probe"]}
    saved = []
    verified = {"TEAMS": "New fixture independently verified"}
    assert offer_local_recovery(config, state, verified, set(), saved.append) == verified
    assert saved == []
    assert offer_local_recovery(config, state, {}, {"TEAMS"}, saved.append) == {"AUTH": SELF_RECOVERY}
    assert saved == [{"smoke_retries": {"AUTH": SELF_RECOVERY}}]
    assert offer_local_recovery(config, state, {"AUTH": SELF_RECOVERY}, set(), saved.append) == {"AUTH": SELF_RECOVERY}
    state["smoke"]["AUTH"]["retry"] = SELF_RECOVERY
    assert offer_local_recovery(config, state, {"AUTH": SELF_RECOVERY}, {"TEAMS"}, saved.append) == {"AUTH": SELF_RECOVERY}
    assert len(saved) == 1
    state["run_meta"]["target"] = "https://dev.example.invalid"
    assert offer_local_recovery(config, state, {}, set(), saved.append) == {}
