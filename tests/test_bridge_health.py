import sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import importlib.util
spec=importlib.util.find_spec('bridge_health')
health=__import__('bridge_health')if spec else None

class HealthTests(unittest.TestCase):
    def setUp(self):self.assertIsNotNone(health,'Layered bridge health missing')
    def test_passive_and_stale_modes_do_not_probe_or_claim_apps_healthy(self):
        with patch.object(health,'probe_windows',side_effect=AssertionError('No implicit guest probe')):
            for stale in(False,True):
                result=health.status({'restart_required':stale,'transport':'stdio'},{'ready':True},{'blocked':False},{'blocked':False})
                self.assertEqual(result['layers']['executor']['state'],'not_probed')
                self.assertIsNone(result['dispatch_preconditions_verified'])
                if stale:self.assertEqual(result['layers']['runtime']['state'],'restart_required')
    def test_probe_keeps_ole_and_accessibility_separate_from_guest_liveness(self):
        with patch.object(health,'probe_windows',return_value={'vm_state':'running','guest_reachable':True,'instances':[]}):
            r=health.status({'restart_required':False},{'ready':True},{'blocked':False},{'blocked':False},probe=True)
        self.assertTrue(r['dispatch_preconditions_verified'])
        self.assertEqual(r['layers']['prism']['state'],'not_running')
        self.assertEqual(r['layers']['ole']['state'],'not_probed')
        self.assertEqual(r['layers']['ui_input']['state'],'not_used')
    def test_active_or_invalid_journal_never_becomes_ready(self):
        with patch.object(health,'probe_windows',return_value={'vm_state':'running','guest_reachable':True,'instances':[]}):
            r=health.status({'restart_required':False},{'ready':True},{'blocked':False},{'blocked':True,'state':'unknown'},probe=True)
        self.assertFalse(r['dispatch_preconditions_verified'])
        self.assertEqual(r['layers']['ppt_queue']['state'],'blocked')

if __name__=='__main__':unittest.main()
