import sys
sys.path.insert(0, '/workspace/af-praxis/agent_factory/src')
sys.path.insert(0, '/workspace/af-praxis/agent_factory/hooks')

import _praxis as p
from _ticket_state import *

plan = project_ref('af-super-run').plan
print('Plan ref:', plan)

vals = [
    {
        "validation_id": "fingerprint-allowlist",
        "covers": ["76e3ba37760647a7ac7d7f7168ed680f::acceptance", "minimalism-dry"],
        "run": "/workspace/af-praxis/.venv/bin/python /workspace/praxis/.claude/worktrees/wf_6bf1c062-02d-6/_eval_fingerprint.py",
    },
]

pin_validations('76e3ba37760647a7ac7d7f7168ed680f', vals, ref=plan)
gap = coverage_gap('76e3ba37760647a7ac7d7f7168ed680f', ref=plan)
print('Coverage gap:', gap)
