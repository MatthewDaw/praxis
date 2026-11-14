from pathlib import Path
import select
import subprocess
import sys

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def start(state, *, integration, cwd=None, slots=2, weighted=False, lightweight=False):
    code = 'import sys; sys.path.insert(0,sys.argv[1]); from af_validation_slots import acquire,acquire_weighted; fn=acquire_weighted if sys.argv[6]=="1" else acquire; extra={"lightweight":sys.argv[7]=="1"} if sys.argv[6]=="1" else {"slots":int(sys.argv[5])}; fds=fn(sys.argv[2],integration=sys.argv[3]=="1",cwd=sys.argv[4] or None,**extra); print("admitted",flush=True); sys.stdin.read(1)'
    return subprocess.Popen([sys.executable, '-c', code, str(SCRIPTS), str(state), '1' if integration else '0', str(cwd or ''), str(slots), "1" if weighted else "0", "1" if lightweight else "0"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def ready(proc, timeout=2):
    available, _, _ = select.select([proc.stdout], [], [], timeout)
    if not available:
        return False
    assert proc.stdout.readline().strip() == 'admitted'
    return True


def release(proc):
    proc.stdin.write('\n')
    proc.stdin.flush()
    proc.communicate(timeout=3)
    assert proc.returncode == 0


def cleanup(*processes):
    for proc in processes:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=3)


def test_two_integrations_overlap_third_waits_and_exclusive_waits_for_both(tmp_path):
    a = start(tmp_path, integration=True)
    b = start(tmp_path, integration=True)
    assert ready(a) and ready(b)
    c = start(tmp_path, integration=True)
    try:
        assert not ready(c, .15)
        release(a)
        assert ready(c)
        exclusive = start(tmp_path, integration=False)
        try:
            assert not ready(exclusive, .15)
            release(b)
            assert not ready(exclusive, .15)
            release(c)
            assert ready(exclusive)
            release(exclusive)
        finally:
            cleanup(exclusive)
    finally:
        cleanup(a, b, c)


def test_same_worktree_serializes_even_with_two_available_slots(tmp_path):
    a = start(tmp_path, integration=True, cwd=tmp_path/'worktree')
    assert ready(a)
    b = start(tmp_path, integration=True, cwd=tmp_path/'worktree')
    try:
        assert not ready(b, .15)
        release(a)
        assert ready(b)
        release(b)
    finally:
        cleanup(a, b)


def test_three_slot_profile_bounds_fourth_and_preserves_exclusive_gate(tmp_path):
    active = [start(tmp_path, integration=True, slots=3) for _ in range(3)]
    fourth = None
    exclusive = None
    try:
        assert all(ready(proc) for proc in active)
        fourth = start(tmp_path, integration=True, slots=3)
        assert not ready(fourth, .15)
        release(active.pop())
        assert ready(fourth)
        exclusive = start(tmp_path, integration=False)
        assert not ready(exclusive, .15)
        for proc in active:
            release(proc)
        assert not ready(exclusive, .15)
        release(fourth)
        assert ready(exclusive)
        release(exclusive)
    finally:
        cleanup(*active, *[p for p in (fourth, exclusive) if p is not None])


def test_queued_exclusive_is_not_starved_by_later_integrations(tmp_path):
    active = start(tmp_path, integration=True, slots=3)
    assert ready(active)
    writer = start(tmp_path, integration=False)
    assert not ready(writer, .2)
    later = start(tmp_path, integration=True, slots=3)
    try:
        assert not ready(later, .2)
        release(active)
        assert ready(writer)
        assert not ready(later, .15)
        release(writer)
        assert ready(later)
        release(later)
    finally:
        cleanup(active, writer, later)


def test_weighted_build_overlaps_one_api_but_reserves_two_tokens(tmp_path):
    api = start(tmp_path, integration=True, weighted=True)
    build = start(tmp_path, integration=False, weighted=True)
    assert ready(api) and ready(build)
    api2 = start(tmp_path, integration=True, weighted=True)
    build2 = start(tmp_path, integration=False, weighted=True)
    try:
        assert not ready(api2, .15)
        assert not ready(build2, .15)
        release(api)
        assert ready(api2)
        assert not ready(build2, .15)
        release(build)
        assert ready(build2)
        release(api2)
        release(build2)
    finally:
        cleanup(api, build, api2, build2)


def test_weighted_three_apis_exclude_a_build_until_two_finish(tmp_path):
    apis = [start(tmp_path, integration=True, weighted=True) for _ in range(3)]
    assert all(ready(p) for p in apis)
    build = start(tmp_path, integration=False, weighted=True)
    try:
        assert not ready(build, .15)
        release(apis.pop())
        assert not ready(build, .15)
        release(apis.pop())
        assert ready(build)
        release(build)
        release(apis.pop())
    finally:
        cleanup(*apis, build)


def test_headroom_discounts_only_inactive_file_cache(tmp_path):
    sys.path.insert(0, str(SCRIPTS))
    from af_validation_slots import working_set_bytes
    (tmp_path/'memory.current').write_text('6000\n')
    (tmp_path/'memory.stat').write_text('anon 2000\ninactive_file 2500\nactive_file 800\nslab_reclaimable 700\n')
    assert working_set_bytes(tmp_path) == 3500
    (tmp_path/'memory.stat').write_text('inactive_file 9999\n')
    assert working_set_bytes(tmp_path) == 0
    (tmp_path/'memory.stat').write_text('invalid accounting')
    assert working_set_bytes(tmp_path) == 6000
    (tmp_path/'memory.stat').unlink()
    assert working_set_bytes(tmp_path) == 6000


def test_lightweight_checks_share_budget_without_browser_lock(tmp_path):
    a = start(tmp_path, integration=False, weighted=True, lightweight=True)
    b = start(tmp_path, integration=False, weighted=True, lightweight=True)
    api = start(tmp_path, integration=True, weighted=True)
    try:
        assert ready(a) and ready(b) and ready(api)
        release(a); release(b); release(api)
    finally:
        cleanup(a, b, api)


def test_only_audited_npm_scripts_get_unit_allocation():
    sys.path.insert(0, str(SCRIPTS))
    from af_validation_slots import validation_kind
    assert validation_kind('vitest.mjs', ['run']) == 'unit'
    assert validation_kind('tsc.js', ['--noEmit']) == 'unit'
    assert validation_kind('npm', ['run', 'test']) == 'build'
    assert validation_kind('npm', ['run', 'test'], unit_scripts={'test'}) == 'unit'
    assert validation_kind('npm', ['test'], unit_scripts={'test'}) == 'unit'
    assert validation_kind('npm', ['run', 'test:ticket'], unit_scripts={'test:ticket'}) == 'unit'
    assert validation_kind('npm', ['run', 'test:e2e'], unit_scripts={'test'}) == 'build'
    assert validation_kind('npm', ['exec', '--', 'playwright', 'test'], unit_scripts={'test'}) == 'build'
    assert validation_kind('test-harness.ts', ['--','npm','run','test:integration']) == 'api'
    assert validation_kind('check-r001-foundation.ts', []) == 'build'


def test_subdirectories_of_one_real_checkout_share_a_lock(tmp_path):
    repo=tmp_path/'repo';repo.mkdir()
    subprocess.run(['git','init',str(repo)],check=True,capture_output=True)
    web=repo/'apps/web';api=repo/'apps/api';web.mkdir(parents=True);api.mkdir()
    state=tmp_path/'state'
    a=start(state,integration=True,cwd=web,weighted=True)
    assert ready(a)
    b=start(state,integration=True,cwd=api,weighted=True)
    try:
        assert not ready(b,.15)
        release(a)
        assert ready(b)
        release(b)
    finally:cleanup(a,b)


def test_linked_worktrees_keep_independent_checkout_locks(tmp_path):
    repo=tmp_path/'repo';repo.mkdir()
    subprocess.run(['git','init',str(repo)],check=True,capture_output=True)
    subprocess.run(['git','-C',str(repo),'-c','user.name=Test','-c','user.email=test@example.test','-c','commit.gpgsign=false','commit','--allow-empty','-m','initial'],check=True,capture_output=True)
    other=tmp_path/'other'
    subprocess.run(['git','-C',str(repo),'worktree','add','--detach',str(other),'HEAD'],check=True,capture_output=True)
    a=start(tmp_path/'state',integration=True,cwd=repo,weighted=True)
    b=start(tmp_path/'state',integration=True,cwd=other,weighted=True)
    try:
        assert ready(a) and ready(b)
        release(a);release(b)
    finally:cleanup(a,b)


def test_reservation_headroom_admits_small_checks_without_overadmitting_builds():
    sys.path.insert(0, str(SCRIPTS))
    from af_validation_slots import reservation_fits
    gib=1024**3
    assert reservation_fits(6*gib,requested_bytes=3*gib,limit_bytes=12*gib)
    assert not reservation_fits(6*gib,requested_bytes=6*gib,limit_bytes=12*gib)
    assert reservation_fits(5*gib,requested_bytes=6*gib,limit_bytes=12*gib)
    assert not reservation_fits(8*gib+1,requested_bytes=3*gib,limit_bytes=12*gib)
