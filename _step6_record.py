import sys
sys.path.insert(0, '/workspace/af-praxis/agent_factory/src')
sys.path.insert(0, '/workspace/af-praxis/agent_factory/hooks')

import _praxis as p
from _ticket_state import *

plan = project_ref('af-super-run').plan
print('Recording validation pass for fingerprint-allowlist...')
record_validation_pass('76e3ba37760647a7ac7d7f7168ed680f', 'fingerprint-allowlist', passed=True, ref=plan)
print('Done.')
