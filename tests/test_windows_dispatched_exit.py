"""No PID observation is safe only with a closed, exact dispatched job."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import windows_prism_executor as executor

class DispatchedExit(unittest.TestCase):
    def test_closed_exact_job_and_live_unknown_changed_boundaries(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);identifier=str(uuid.uuid4());job=root/'PrismBridge/jobs'/identifier
            (job/'input').mkdir(parents=True);script=root/'phase.pzc'
            raw=b'CreateLog\nSetPath "stage"\n';script.write_bytes(raw)
            (job/'input/phase.pzc').write_bytes(raw);digest=hashlib.sha256(raw).hexdigest()
            policy={'shared_root':str(root),'prism_exe':'Prism.exe','prism_sha256':'a'*64}
            request={'id':identifier,'script':'phase.pzc','hashes':{'phase.pzc':digest},
                     'prism_exe':policy['prism_exe'],'prism_sha256':policy['prism_sha256']}
            receipt={'id':identifier,'state':'unknown','local_stage':'owned-stage'}
            (job/'request.json').write_text(json.dumps(request));(job/'receipt.json').write_text(json.dumps(receipt))
            (job/'dispatch.claim').write_text('2345')
            script.with_suffix('.windows-job.json').write_text(json.dumps({'id':identifier,'job':str(job)}))
            record={'script':str(script),'script_sha256':digest}
            good={'id':identifier,'helper_state':'exited','available':True,'instances':[],
                  'own_stage_observed_in_native_log':True,'local_stage':'owned-stage'}
            with patch.object(executor,'guest_call',return_value=good):
                self.assertTrue(executor.dispatched_exit_proof(record,policy))
            for changed in ({'helper_state':'alive'},{'helper_state':'unknown'},{'instances':[{'pid':123}]},
                            {'available':False},{'own_stage_observed_in_native_log':False},
                            {'local_stage':'foreign-stage'},{'id':str(uuid.uuid4())}):
                with patch.object(executor,'guest_call',return_value={**good,**changed}):
                    self.assertFalse(executor.dispatched_exit_proof(record,policy))
            script.write_bytes(raw+b'// changed')
            with patch.object(executor,'guest_call') as guest:
                self.assertFalse(executor.dispatched_exit_proof(record,policy));guest.assert_not_called()

if __name__=='__main__':unittest.main()

