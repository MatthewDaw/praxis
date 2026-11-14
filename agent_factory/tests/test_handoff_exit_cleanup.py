from pathlib import Path
import subprocess
import sys

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/af-ticket-loop.sh'


def run_cleanup(tmp_path, pending):
    text=SCRIPT.read_text()
    fn='af_cleanup_on_exit(){'+text.split('af_cleanup_on_exit(){',1)[1].split('\n# BUG B-5',1)[0]
    (tmp_path/'af_live_handoff.py').write_text('import sys\nsys.exit('+('0' if pending else '1')+')\n')
    (tmp_path/'.git').mkdir()
    script=tmp_path/'run.sh'
    script.write_text('''#!/bin/bash
PY="$1"; WT="$2"
say(){ echo "$*"; }
sweep_worktrees(){ echo SWEEP; }
reap_branches(){ echo REAP; }
af_surface_flags(){ echo FLAGS; }
af_end_of_run_summary(){ echo SUMMARY; }
af_release_worktree_lock(){ echo RELEASE; }
'''+fn+'''\nfalse
af_cleanup_on_exit
result=$?
echo "RESULT=$result"
''')
    return subprocess.run(['bash',str(script),sys.executable,str(tmp_path)],capture_output=True,text=True,timeout=3)


def test_valid_live_handoff_skips_destructive_exit_cleanup_but_releases_lock(tmp_path):
    result=run_cleanup(tmp_path,True)
    assert result.returncode==0,result.stderr
    assert 'RELEASE' in result.stdout and 'RESULT=1' in result.stdout
    assert all(token not in result.stdout for token in ['SWEEP','REAP','FLAGS','SUMMARY'])


def test_ordinary_exit_still_performs_cleanup_and_releases_lock(tmp_path):
    result=run_cleanup(tmp_path,False)
    assert result.returncode==0,result.stderr
    assert result.stdout.splitlines()==['SWEEP','REAP','FLAGS','SUMMARY','RELEASE','RESULT=1']
