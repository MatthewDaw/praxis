import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/af_resume_context.py'
spec=importlib.util.spec_from_file_location('resume_context',SCRIPT)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
SID='01a1d795-cb84-7fe1-abe8-1ea634931555'
PROMPT='/af-build example R008 — build exactly this ONE ticket yourself, inline.'


@pytest.fixture
def receipt(tmp_path):
    folder=tmp_path/'retired-workers';folder.mkdir()
    rollout=tmp_path/'rollout.jsonl';rollout.write_text(json.dumps({'payload':{'id':SID}})+'\n')
    data={'ticket':'R008','tmux_session':'af-example-R008','codex_session_id':SID,'rollout':str(rollout),'terminal_state':'blocked'}
    path=folder/'R008.json';path.write_text(json.dumps(data));path.chmod(0o600)
    return tmp_path,path,data


def test_blocked_worker_resumes_original_conversation_with_driver_flags(receipt):
    state,_,_=receipt
    args=['--dangerously-bypass-approvals-and-sandbox','--no-alt-screen','-c','model_reasoning_effort=high','--',PROMPT]
    assert module.resume_arguments(args,state,'example')==args[:-2]+['resume',SID,'--',PROMPT]


@pytest.mark.parametrize('mutation',[
    {'ticket':'R001'},{'tmux_session':'af-other-R008'},
    {'codex_session_id':'bad; command'},{'terminal_state':'in_progress'},
    {'rollout':'/does/not/exist'},
])
def test_unproven_context_is_not_selected(receipt,mutation):
    state,path,data=receipt;data.update(mutation);path.write_text(json.dumps(data))
    assert module.resume_session(state,'example','R008')==''


def test_context_must_be_private_and_match_original_rollout(receipt):
    state,path,data=receipt;path.chmod(0o644)
    assert module.resume_session(state,'example','R008')==''
    path.chmod(0o600);Path(data['rollout']).write_text(json.dumps({'payload':{'id':'different'}}))
    assert module.resume_session(state,'example','R008')==''


@pytest.mark.parametrize('args',[
    ['exec','--',PROMPT],['resume',SID,'--',PROMPT],['--help'],
    ['--','/af-build other R008 — build exactly this ONE ticket'],
    ['--','Post-merge verification of build round 1'],
])
def test_non_worker_commands_are_unchanged(receipt,args):
    state,_,_=receipt
    assert module.resume_arguments(args,state,'example')==args


def test_explicit_project_supports_a_different_worktree_session_prefix(receipt):
    state,path,data=receipt
    data.update(project='example',tmux_session='af-different-worktree-R008')
    path.write_text(json.dumps(data))
    assert module.resume_session(state,'example','R008')==SID
    assert module.resume_session(state,'other-project','R008')==''
