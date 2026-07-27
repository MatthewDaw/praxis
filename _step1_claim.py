import sys
sys.path.insert(0, '/workspace/af-praxis/agent_factory/src')
sys.path.insert(0, '/workspace/af-praxis/agent_factory/hooks')

import _praxis as p
from _ticket_state import *

result = start_ticket('76e3ba37760647a7ac7d7f7168ed680f', 'af-build/praxis:76e3ba37760647a7ac7d7f7168ed680f', 'af-super-run', override=('af-super-run', 'building-validation'))
if result is None:
    print('BLOCKED: ticket taken or under-specified')
else:
    print('CLAIMED. Resolved requirements:', len(result))
    for r in result:
        print(f'  req: {r.get("id","?")} — {r.get("title","?")[:100]}')
