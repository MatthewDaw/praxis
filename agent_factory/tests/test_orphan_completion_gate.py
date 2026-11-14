"""Execute the real orphan queue: active branches must never reach the resolver."""
from pathlib import Path
import subprocess

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/af-ticket-loop.sh'


def queue(tmp_path, *, outage='', finished='R002 R005', mixed='R005 R001'):
    text = SCRIPT.read_text()
    function = 'queue_orphan_branches(){' + text.split('queue_orphan_branches(){', 1)[1].split('\n# ------------------------------------------------------------------------- conflict resolution', 1)[0]
    harness = r'''
set -euo pipefail
WT="$1"; CONFLICTS="$1/conflicts"; : > "$CONFLICTS"
say(){ echo "$*"; }
praxis_q(){
  [ "$1" != "OUTAGE" ] || return 1
  case "$1" in finished_ids) echo 'FINISHED';; known_ids) echo 'R001 R002 R005 R010';; esac
}
af_is_owed_merge(){ return 0; }
af_branch_has_own_work(){ return 0; }
af_base_ref(){ return 1; }
af_owned_ids(){
  case "$1" in HEAD..done) echo R002;; HEAD..active) echo R010;; HEAD..mixed) echo 'MIXED';; HEAD..unknown) echo '';; esac
}
git(){ case "$1" in merge-base) return 1;; for-each-ref) printf '%s\n' done active mixed unknown;; esac; }
queue_orphan_branches
printf '\nQUEUED\n'; cat "$CONFLICTS"
'''.replace('OUTAGE', outage).replace('FINISHED', finished).replace('MIXED', mixed)
    return subprocess.run(['bash','-c',function+'\n'+harness,'test',str(tmp_path)], capture_output=True,text=True,timeout=3)


def test_only_proven_finished_branches_reach_conflict_resolution(tmp_path):
    result = queue(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split('QUEUED\n', 1)[1].strip() == 'done\tR002'
    assert 'active — ticket(s) not all finished' in result.stdout
    assert 'mixed — ticket(s) not all finished' in result.stdout
    assert 'unknown — no owned ticket provenance' in result.stdout


def test_all_ticket_ids_in_a_mixed_branch_must_be_finished(tmp_path):
    result = queue(tmp_path, finished='R001 R002 R005')
    assert result.returncode == 0, result.stderr
    assert result.stdout.split('QUEUED\n', 1)[1].strip().splitlines() == ['done\tR002','mixed\tR005 R001']


def test_either_provenance_query_outage_retains_every_branch(tmp_path):
    for name in ['finished_ids','known_ids']:
        result = queue(tmp_path, outage=name)
        assert result.returncode == 0, result.stderr
        assert 'deferred' in result.stdout
        assert not result.stdout.split('QUEUED\n', 1)[1].strip()
