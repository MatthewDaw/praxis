import sys
sys.path.insert(0, '/workspace/af-praxis/agent_factory/src')
sys.path.insert(0, '/workspace/af-praxis/agent_factory/hooks')

import _praxis as p
plan = ('af-super-run', 'prd-af-super-run')
fact = p.get_fact('76e3ba37760647a7ac7d7f7168ed680f', space=plan[0], snapshot=plan[1])
meta = fact.get('meta', {})
print('TITLE:', fact.get('title'))
print('ACCEPTANCE:', meta.get('acceptance'))
print('TAGS:', meta.get('tags'))
print('VERIFY:', meta.get('verify'))
print('DEFINES:', meta.get('defines'))
print('REFERENCES:', meta.get('references'))
print('DEPENDS_ON:', meta.get('depends_on'))
print()
print('BODY:', fact.get('content', '')[:2000])
