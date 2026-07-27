import sys
sys.path.insert(0, '/workspace/af-praxis/agent_factory/src')
sys.path.insert(0, '/workspace/af-praxis/agent_factory/hooks')

import _praxis as p
from _ticket_state import *
result = retrieve_advisory_checks('76e3ba37760647a7ac7d7f7168ed680f', 'af-super-run', scope='validation', override=('af-super-run', 'building-validation'))
print('Advisory checks:', len(result) if result else 0)
for c in (result or []):
    print(f'  {c.get("id","?")}: {c.get("title","?")[:80]}')
