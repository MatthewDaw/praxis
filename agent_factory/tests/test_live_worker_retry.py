import importlib.util,sys,tempfile,os,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch,Mock
root=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(root))
import af_live_handoff as h
spec=importlib.util.spec_from_file_location('cleanup',root/'af-stop-owned-scopes.py'); c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
class Tests(unittest.TestCase):
 def test_prepare_consumes_once(self):
  with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'AF_STATE_DIR':d,'FACTORY_PROJECT':'demo','AF_SERVICE_UNIT':'owner.service'},clear=True),patch.object(h.subprocess,'run',return_value=NS(returncode=0)):
   self.assertTrue(h.prepare(['R001'],['af-demo-R001']));self.assertEqual(h.handoff_path().stat().st_mode&0o777,0o600)
   self.assertTrue(h.skip_cleanup_once());self.assertIn('R001',h.consume());self.assertEqual(h.consume(),'')
 def test_inactive_refuses(self):
  with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'AF_STATE_DIR':d,'FACTORY_PROJECT':'demo','AF_SERVICE_UNIT':'owner.service'},clear=True),patch.object(h.subprocess,'run',return_value=NS(returncode=1)):
   self.assertFalse(h.prepare(['R001'],['af-demo-R001']));self.assertFalse(h.handoff_path().exists());self.assertFalse(h.prepare(['R001'],[]))
 def test_only_live_unfinished_factory_workers(self):
  with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'AF_STATE_DIR':d,'FACTORY_PROJECT':'demo','AF_SESSION_PREFIX':'af-demo'},clear=True):
   for ticket in ['R001','R002','R003']:
    p=Path(d)/('af-af-demo-'+ticket+'.prompt');p.write_text('assignment');p.chmod(0o600)
   def run(cmd,**kw):
    if 'is-active' in cmd:return NS(returncode=0)
    if 'list-sessions' in cmd:return NS(stdout='af-demo-R001\naf-demo-R002\naf-demo-R003\naf-demo-verify\n')
    return NS(stdout='bash' if 'af-demo-R003' in cmd else 'claude')
   facts=[{'meta':{'requirement_id':t,'build_state':s}} for t,s in [('R001','in_progress'),('R002','finished'),('R003','incomplete')]]
   with patch.dict(sys.modules,{'_praxis':NS(facts_by=Mock(return_value=facts))}),patch.object(c.subprocess,'run',side_effect=run),patch.object(h,'prepare',return_value=True) as prepare:
    self.assertTrue(c.preserve_active_workers('owner.service'));prepare.assert_called_once_with(['R001'],['af-demo-R001'])
 def test_explicit_stop_not_preserved(self):
  with patch.dict(os.environ,{'AF_STATE_DIR':'/tmp','FACTORY_PROJECT':'demo'},clear=True),patch.object(c.subprocess,'run',return_value=NS(returncode=3)):
   self.assertFalse(c.preserve_active_workers('owner.service'))
if __name__=='__main__':unittest.main()
