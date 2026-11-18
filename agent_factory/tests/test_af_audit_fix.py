import copy
import hashlib

import pytest

from agent_factory.af_audit_fix import Audit


_ARTIFACT = {}


@pytest.fixture(autouse=True)
def retained_evidence(tmp_path):
    path = tmp_path / "team.png"
    path.write_bytes(b"actual browser screenshot bytes")
    _ARTIFACT.clear()
    _ARTIFACT.update(uri=path.as_uri(), sha256=hashlib.sha256(path.read_bytes()).hexdigest())


class MemoryPraxis:
    def __init__(self):
        self.rows = {}
        self.next_id = 0

    def ensure_space(self, *args, **kwargs):
        pass

    def facts_by(self, category, meta, state, space, snapshot):
        return copy.deepcopy([r for (s, n, _), r in self.rows.items() if s == space and n == snapshot
                              and all(r['meta'].get(k) == v for k, v in meta.items())])

    def _request(self, method, path, body, space, snapshot):
        assert (method, path) == ('POST', '/insights')
        assert body['raw'] is True  # no semantic dedup of similarly named distinct cases
        self.next_id += 1
        row = {'id': str(self.next_id), 'meta': copy.deepcopy(body['meta'])}
        self.rows[(space, snapshot, row['id'])] = row
        return {'id': row['id']}

    def patch_meta(self, cid, data, space, snapshot):
        self.rows[(space, snapshot, cid)]['meta'].update(copy.deepcopy(data))


def evidence(status='passed', build='sha1'):
    return {'status': status, 'build': build, 'role': 'admin', 'route': '/teams',
            'steps': ['Click add team', 'Enter a name', 'Save', 'Reload'],
            'expected': 'Team persists', 'actual': 'Team persists' if status == 'passed' else 'Save returns 500',
            'evidence': [copy.deepcopy(_ARTIFACT)],
            'viewport': '1280x720', 'console': [], 'network': [], 'cleanup': 'No mutations',
            **({'blocker': 'Missing payment sandbox'} if status == 'blocked' else {})}


@pytest.fixture
def audit():
    a = Audit('sports', 'run-1', MemoryPraxis())
    a.init({'scenarios': [{'id': 'A', 'title': 'Create team'}, {'id': 'B', 'title': 'Read team'}]},
           'https://example.test', 'sha1')
    return a


def freeze(a):
    a.advance({'observed_build': 'sha1', 'discovery_reconciled': True, 'discovery_evidence': 'trace:index'})


def deploy(a):
    a.advance({'observed_build': 'sha1', 'fixes': {'A': {'commit': 'sha2', 'validation': 'test:regression'}}})
    a.advance({'observed_build': 'sha1', 'deployment': {'target': 'https://example.test', 'build': 'sha2',
                'healthy': True, 'migrations_verified': True, 'evidence': 'release:sha2'}})


def test_no_page_load_pass_or_incomplete_freeze(audit):
    with pytest.raises(ValueError, match='unexecuted'):
        freeze(audit)
    bad = evidence()
    bad.pop('steps')
    with pytest.raises(ValueError, match='steps'):
        audit.record('A', bad)
    assert audit.status()['counts']['not_run'] == 2


def test_build_drift_and_evidence_hash_required(audit):
    with pytest.raises(ValueError, match='drift'):
        audit.record('A', evidence(build='other'))
    bad = evidence()
    bad['evidence'][0]['sha256'] = 'fake'
    with pytest.raises(ValueError, match='SHA256'):
        audit.record('A', bad)


def test_frozen_failures_and_deployment_gate(audit):
    audit.record('A', evidence('failed'))
    audit.record('B', evidence())
    freeze(audit)
    assert audit.status()['failed_ids'] == ['A']
    with pytest.raises(ValueError, match='only writable'):
        audit.record('A', evidence())
    with pytest.raises(ValueError, match='every frozen failure'):
        audit.advance({'observed_build': 'sha1', 'fixes': {}})
    deploy(audit)
    with pytest.raises(ValueError, match='frozen initial failures'):
        audit.record('B', evidence(build='sha2'))
    audit.record('A', evidence('failed', 'sha2'))
    with pytest.raises(ValueError, match='must pass'):
        audit.advance({'observed_build': 'sha2', 'cleanup_verified': True, 'cleanup_evidence': 'cleanup:all'})
    with pytest.raises(ValueError, match='rerun_reason'):
        audit.record('A', evidence(build='sha2'))
    audit.record('A', {**evidence(build='sha2'), 'rerun_reason': 'Repeat after correcting the fixture'})
    audit.advance({'observed_build': 'sha2', 'cleanup_verified': True, 'cleanup_evidence': 'cleanup:all'})
    assert audit.status()['phase'] == 'complete'
    assert audit.one('case:A')['meta']['initial']['status'] == 'failed'
    assert len(audit.one('case:A')['meta']['reaudit_attempts']) == 2


def test_blocked_never_green_and_partial_completion(audit):
    audit.record('A', evidence('failed'))
    audit.record('B', evidence('blocked'))
    freeze(audit)
    deploy(audit)
    audit.record('A', evidence(build='sha2'))
    audit.advance({'observed_build': 'sha2', 'cleanup_verified': True, 'cleanup_evidence': 'cleanup:all'})
    assert audit.status()['phase'] == 'partial'
    assert not audit.status()['full_coverage']
    assert audit.status()['counts']['blocked'] == 1


def test_isolated_run_scope_and_retry_init(audit):
    other = Audit('foreign', 'run-1', audit.client)
    with pytest.raises(ValueError, match='found 0'):
        other.status()
    status = audit.init({'scenarios': [{'id': 'A', 'title': 'Create team'}, {'id': 'B', 'title': 'Read team'}]},
                       'https://example.test', 'sha1')
    assert status['counts']['not_run'] == 2
    with pytest.raises(ValueError, match='different inventory'):
        audit.init({'scenarios': [{'id': 'C'}]}, 'https://example.test', 'sha1')


def test_discovered_cases_freeze_and_credential_rejection(audit):
    audit.add_case({'id': 'C', 'title': 'Newly discovered control', 'discovered_control': 'New control', 'behavior_id': 'new-control', 'role': 'admin', 'sport': 'n/a', 'viewport': '1280x720'})
    bad = evidence()
    bad['actual'] = 'password=DoNotUpload123'
    with pytest.raises(ValueError, match='credentials'):
        audit.record('A', bad)
    for case in ['A', 'B', 'C']:
        audit.record(case, {**evidence(), **({'sport': 'n/a'} if case == 'C' else {})})
    freeze(audit)
    with pytest.raises(ValueError, match='frozen'):
        audit.add_case({'id': 'D'})


def test_cleanup_and_healthy_migrations_required(audit):
    audit.record('A', evidence('failed'))
    audit.record('B', evidence())
    freeze(audit)
    audit.advance({'observed_build': 'sha1', 'fixes': {'A': {'commit': 'sha2', 'validation': 'test'}}})
    with pytest.raises(ValueError, match='migration'):
        audit.advance({'observed_build': 'sha1', 'deployment': {'build': 'sha2', 'healthy': True,
                                                             'target': 'https://example.test'}})


def test_failed_reaudit_returns_to_fix_and_neighbors_cannot_hide_failures(audit):
    audit.record('A', evidence('failed'))
    audit.record('B', evidence())
    freeze(audit)
    deploy(audit)
    audit.add_case({'id': 'NEIGHBOR', 'regression_for': 'A', 'title': 'List teams still works'})
    with pytest.raises(ValueError, match='cannot replace'):
        audit.add_case({'id': 'A', 'regression_for': 'A'})
    audit.record('A', evidence('failed', 'sha2'))
    audit.record('NEIGHBOR', evidence(build='sha2'))
    audit.advance({'observed_build': 'sha2', 'retry_fixes': True})
    assert audit.status()['phase'] == 'fix'
    audit.advance({'observed_build': 'sha2', 'fixes': {'A': {'commit': 'sha3', 'validation': 'test:retry'}}})
    audit.advance({'observed_build': 'sha2', 'deployment': {'target': 'https://example.test', 'build': 'sha3',
                  'healthy': True, 'migrations_verified': True, 'evidence': 'release:sha3'}})
    audit.record('A', evidence(build='sha3'))
    with pytest.raises(ValueError, match='adjacent'):
        audit.advance({'observed_build': 'sha3', 'cleanup_verified': True, 'cleanup_evidence': 'cleanup:all'})
    audit.record('NEIGHBOR', evidence(build='sha3'))
    audit.advance({'observed_build': 'sha3', 'cleanup_verified': True, 'cleanup_evidence': 'cleanup:all'})
    assert audit.status()['phase'] == 'complete'
    assert audit.one('run')['meta']['failed_ids'] == ['A']
    assert audit.one('run')['meta']['deployment_history'][0]['build'] == 'sha2'


def test_wrong_origin_and_expanded_role_sport_viewport_rejected(audit):
    audit.add_case({'id': 'TUPLE', 'parent_id': 'A', 'behavior_id': 'create-team', 'required_role': 'official', 'required_sport': 'basketball',
                    'required_viewport': '1280x720'})
    wrong = evidence()
    wrong['route'] = 'https://unrelated.example/teams'
    with pytest.raises(ValueError, match='origin'):
        audit.record('A', wrong)
    with pytest.raises(ValueError, match='role'):
        audit.record('TUPLE', evidence())
    wrong = evidence()
    wrong['role'] = 'official'
    wrong['sport'] = 'baseball'
    with pytest.raises(ValueError, match='sport'):
        audit.record('TUPLE', wrong)
    wrong['sport'] = 'basketball'
    wrong['viewport'] = '320x480'
    with pytest.raises(ValueError, match='viewport'):
        audit.record('TUPLE', wrong)
    wrong['viewport'] = '1280x720'
    audit.record('TUPLE', wrong)


def test_evidence_bytes_exist_and_hash_matches(audit, tmp_path):
    wrong = evidence()
    wrong['evidence'][0]['uri'] = (tmp_path / 'nonexistent.png').as_uri()
    with pytest.raises(ValueError, match='does not exist'):
        audit.record('A', wrong)
    wrong = evidence()
    wrong['evidence'][0]['sha256'] = 'b' * 64
    with pytest.raises(ValueError, match='mismatch'):
        audit.record('A', wrong)
    wrong = evidence()
    wrong['evidence'][0]['uri'] = 'https://example.test/evidence.png'
    with pytest.raises(ValueError, match='evidence_path'):
        audit.record('A', wrong)
    wrong['evidence'][0]['evidence_path'] = str(tmp_path / 'bytes.png')
    (tmp_path / 'bytes.png').write_bytes(b'actual browser screenshot bytes')
    audit.record('A', wrong)


def test_report_contains_reaudit_deployment_and_cleanup(audit):
    audit.record('A', evidence('failed'))
    audit.record('B', evidence())
    freeze(audit)
    deploy(audit)
    audit.add_case({'id': 'NEIGHBOR', 'regression_for': 'A', 'title': 'List teams'})
    audit.record('A', evidence(build='sha2'))
    audit.record('NEIGHBOR', evidence(build='sha2'))
    audit.advance({'observed_build': 'sha2', 'cleanup_verified': True, 'cleanup_evidence': 'cleanup:all'})
    report = audit.report()
    assert 'Re-audit attempt 1: passed' in report
    assert 'Adjacent regression for: A' in report
    assert 'release:sha2' in report
    assert 'cleanup:all' in report
    assert _ARTIFACT['sha256'] in report

def test_reuse_and_partial_inventory_coverage(audit):
    case = dict(id="C", parent_id="A", behavior_id="create-team", role="admin",
                sport="n/a", viewport="1280x720")
    audit.add_case(case)
    with pytest.raises(ValueError, match="reuse C"):
        audit.add_case({**case, "id": "D", "behavior_id": " Create-team "})
    assert not audit.check("C")["reuse"]
    audit.record("C", {**evidence(), "sport": "n/a"})
    assert audit.check("C")["reuse"]
    assert audit.check("A")["linked_evidence"][0]["case_id"] == "C"
    assert audit.status()["inventory_counts"] == dict(untouched=1, partial=1, blocked=0, passed=0, failed=0)
    with pytest.raises(ValueError, match="unexecuted"):
        freeze(audit)


def test_provisioning_cannot_freeze_stale_identity_blocks(audit):
    audit.record('A', evidence('blocked'))
    audit.record('B', evidence())
    audit.patch(audit.one('run'), {'identity_prerequisite': {'status': 'available', 'revision': 'fixture2',
                                                          'affected_original_ids': ['A']}})
    with pytest.raises(ValueError, match='reconsider previously blocked'):
        freeze(audit)
    # A real newly observed external prerequisite can remain partial after rechecking.
    renewed = evidence('blocked')
    renewed['prerequisite_revision'] = 'fixture2'
    renewed['blocking_prerequisites'] = ['controlled-inbox']
    renewed['blocker'] = 'Sign-in works; controlled reset inbox not configured'
    audit.record('A', renewed)
    freeze(audit)
    assert audit.status()['phase'] == 'fix'


def test_external_dependency_change_prevents_freezing_unchanged_blocker(audit):
    from agent_factory.af_audit_fix import digest
    old = evidence('blocked')
    old.update(prerequisite_revision='fixture2', blocking_prerequisites=['controlled-inbox'])
    audit.record('A', old)
    audit.record('B', evidence())
    audit.patch(audit.one('run'), {'identity_prerequisite': {
        'status': 'available', 'revision': 'fixture2', 'affected_original_ids': ['A'],
        'external_deferred': {'A': {'result_fingerprint': digest(old),
                                   'dependency_fingerprint': digest({'controlled-inbox': {'status': 'missing'}})}}},
        'audit_prerequisites': {'controlled-inbox': {'status': 'available'}}})
    with pytest.raises(ValueError, match='prerequisite changes'):
        freeze(audit)
    new = evidence('blocked')
    new.update(prerequisite_revision='fixture2', blocking_prerequisites=['controlled-inbox'],
               actual='New issued reset link works, outbound inbox delivery still unverified')
    audit.record('A', new)
    freeze(audit)


def test_batch_check_reuses_one_status_snapshot(audit, monkeypatch):
    status = audit.status()
    reads = []
    def read():
        reads.append(True)
        return status
    monkeypatch.setattr(audit, "status", read)
    checks = audit.check_many(["B", "A"])
    assert [c["case_id"] for c in checks] == ["B", "A"]
    assert len(reads) == 1


def test_receipt_avoids_full_status_read_after_record(audit, monkeypatch):
    monkeypatch.setattr(audit, 'status', lambda: pytest.fail("unexpected inventory read"))
    result = evidence()
    receipt = audit.record("A", result, receipt=True)
    assert receipt == {"case_id": "A", "phase": "audit", "build": "sha1", "status": "passed"}
    assert audit.record("A", result, receipt=True) == receipt


def test_sweep_replay_scope_and_close_first(audit, tmp_path):
    import json
    from agent_factory.af_audit_queue import audit_queue, next_visits
    path = tmp_path / "sweep.json"
    action = dict(id="save", caseId="A", action="save", expected="saved", actual="saved", verified=True,
                  equivalentRoles=["coach"], equivalenceBasis="same scoped display")
    report = dict(origin="https://example.test", build="sha1", role="admin", sport="soccer", viewport="desktop",
                  fixtureState="owned", batchStartedAt=1, browserStartedAt=2, elapsedMs=3,
                  visits=[dict(path="/teams", actions=[action])], remaining={"A": ["reload"]})
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="shared checks"):
        audit.record_sweep(path)
    action["permissionBoundary"] = False
    path.write_text(json.dumps(report))
    for _ in range(2):
        audit.record_sweep(path)
    status = audit.status()
    assert len(status["sweeps"]) == 1
    status["cases"][0]["scenario"].update(roles=["coach", "outsider"], route="/teams")
    q = audit_queue(status)
    first = next_visits(status, q)[0]
    assert first["close_first"] == ["A"]
    assert [x["role"] for x in first["case_focus"]["A"]["verified_checks"]] == ["coach"]
    assert q["progress"]["completed"] == 0


def test_record_batch_validates_all_verdicts_before_writing(audit):
    a = {"case_id": "A", "result": evidence()}
    b = {"case_id": "B", "result": evidence(build="wrong-build")}
    with pytest.raises(ValueError, match="drift"):
        audit.record_batch([a, b])
    assert audit.status()["counts"]["not_run"] == 2
    b["result"]["build"] = "sha1"
    assert audit.record_batch([a, b]) == audit.record_batch([a, b])
    assert audit.status()["counts"]["passed"] == 2
