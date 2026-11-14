from pathlib import Path
import subprocess

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/af-ticket-loop.sh'


def run_refill(tmp_path, count):
    text=SCRIPT.read_text();fn=text.split('af_rolling_refill(){',1)[1].split('\naf_wait_verify(){',1)[0]
    code='af_rolling_refill(){'+fn
    harness='''
AF_ROLLING=1; BACKEND=codex; PROJECT=example; SESSION=af-example
AF_STATE_DIR="$1"; BATCH_MAX=4; MAX=0; n=0; round=1; READY_POLL_MAX=1
AF_CARRY_IDS='R006 R007 R008'; AF_CARRY_SESSIONS='s6 s7 s8'; AF_ROUND_IDS='R001 R002 R003 R005'
say(){ echo "$*"; }
af_count(){ set -- $1; echo $#; }
af_in_list(){ case " $2 " in *" $1 "*) return 0;; esac; return 1; }
af_resource_cap(){ echo 4; }
af_dependencies_advisory(){ return 0; }
praxis_q(){ case "$1" in batch_open) COUNT_RESPONSE;; ready_batch) echo R009;; any_batch) echo R010;; esac; }
tmux(){ return 1; }
af_worker_prompt(){ echo "$1"; }
af_launch_agent(){ echo "LAUNCH $1"; }
af_wait_ready(){ return 0; }
af_rolling_refill 1
printf 'CARRIED=%s\\n' "$AF_CARRY_IDS"
'''.replace('COUNT_RESPONSE',count)
    return subprocess.run(['bash','-c',code+'\n'+harness,'test',str(tmp_path)],capture_output=True,text=True,timeout=3)


def test_terminal_carried_ticket_frees_slot_but_remains_for_integration(tmp_path):
    r=run_refill(tmp_path,'echo 2')
    assert r.returncode==0,r.stderr
    assert 'LAUNCH af-example-R009' in r.stdout
    assert 'CARRIED=R006 R007 R008 R009' in r.stdout


def test_live_carried_workers_fill_capacity(tmp_path):
    r=run_refill(tmp_path,'echo 3')
    assert r.returncode==0,r.stderr
    assert 'LAUNCH' not in r.stdout


def test_failed_state_observation_never_overadmits(tmp_path):
    r=run_refill(tmp_path,'return 1')
    assert r.returncode==0,r.stderr
    assert 'LAUNCH' not in r.stdout
    assert 'deferred' in r.stdout
