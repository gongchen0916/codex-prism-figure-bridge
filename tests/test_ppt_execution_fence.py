import json,sys,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import prism_bridge as b
import windows_prism_ppt as ppt

class PPTFenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.policy={'shared_root':str(self.root/'share'),'ready':True}
        self.project=self.root/'input.pzfx';self.project.write_text('synthetic native input')
        for target,name,value in ((b,'PRISM_LOCK_PATH',self.root/'lock'),(b,'execution_status',lambda:{'blocked':False,'state':'idle'})):
            p=patch.object(target,name,value);p.start();self.addCleanup(p.stop)
    def test_ppt_timeout_fences_native_and_second_ppt_dispatch(self):
        def timeout(policy,action,job,**kwargs):
            (job/'ppt.claim').write_text('123');raise TimeoutError('test OLE timeout')
        with patch.object(ppt.executor,'guest_call',side_effect=timeout)as call:
            with self.assertRaises(TimeoutError):ppt.embed(self.project,self.root/'first',policy=self.policy)
            with self.assertRaises((RuntimeError,TimeoutError)):b._check_execution_ready()
            with self.assertRaises((RuntimeError,TimeoutError)):ppt.embed(self.project,self.root/'second',policy=self.policy)
            self.assertEqual(call.call_count,1)
    def test_late_receipt_requires_complete_files_and_exact_operation(self):
        self.assertTrue(hasattr(ppt,'execution_status'))
        job=None
        def timeout(policy,action,path,**kwargs):
            nonlocal job;job=path;(path/'ppt.claim').write_text('123');raise TimeoutError('test')
        with patch.object(ppt.executor,'guest_call',side_effect=timeout):
            with self.assertRaises(TimeoutError):ppt.embed(self.project,self.root/'first',policy=self.policy)
        request=json.loads((job/'ppt-request.json').read_text())
        receipt={'id':str(uuid.uuid4()),'state':'saved_reopened','source_sha256':request['source_sha256'],'size_verified':True,
            'delivery_type':'embedded_prism_ole','scale':1.0,'click_action':{'saved_reopened_verified':True}}
        for name in ('prism_editable.pptx','slide-before.png','slide-reopened.png'):(job/name).write_bytes(b'proof')
        (job/'ppt-receipt.json').write_text(json.dumps(receipt));self.assertTrue(ppt.execution_status()['blocked'])
        receipt['id']=request['id'];(job/'ppt-receipt.json').write_text(json.dumps(receipt))
        self.assertTrue(ppt.execution_status()['blocked'],'Early saved receipt must not release active worker')
        (job/'ppt-worker-finished.json').write_text(json.dumps({'id':request['id'],'finished':True}))
        self.assertFalse(ppt.execution_status()['blocked'])
        (job/'slide-reopened.png').unlink();self.assertTrue(ppt.execution_status()['blocked'])
    def test_activation_early_saved_receipt_remains_fenced(self):
        def pending(policy,action,job,**kwargs):
            request=json.loads((job/'ppt-request.json').read_text());(job/'ppt.claim').write_text('123')
            r={'id':request['id'],'state':'saved_reopened','source_sha256':request['source_sha256'],'size_verified':True,
                'delivery_type':'embedded_prism_ole','scale':1.0,'click_action':{'saved_reopened_verified':True}}
            (job/'ppt-receipt.json').write_text(json.dumps(r))
            for name in('prism_editable.pptx','slide-before.png','slide-reopened.png'):(job/name).write_bytes(b'proof')
            raise TimeoutError('DoVerb pending')
        with patch.object(ppt.executor,'guest_call',side_effect=pending):
            with self.assertRaises(TimeoutError):ppt.embed(self.project,self.root/'interactive',activate=True,policy=self.policy)
        self.assertTrue(ppt.execution_status()['blocked'])
    def test_mutual_exclusion_uses_existing_native_lock(self):
        entered=[]
        from contextlib import contextmanager
        @contextmanager
        def lock(**kwargs):entered.append(True);yield
        with patch.object(b,'prism_export_lock',lock),patch.object(ppt.executor,'guest_call',side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):ppt.embed(self.project,self.root/'first',policy=self.policy)
        self.assertEqual(entered,[True])
    def test_recovery_needs_exact_exited_helper_and_no_active_apps(self):
        def timeout(policy,action,job,**kwargs):
            (job/'ppt.claim').write_text('123');raise TimeoutError('test')
        with patch.object(ppt.executor,'guest_call',side_effect=timeout):
            with self.assertRaises(TimeoutError):ppt.embed(self.project,self.root/'first',policy=self.policy)
        ident=ppt.execution_status()['operation_id']
        for response in ({'id':ident,'available':True,'helper_state':'alive','instances':[]},
            {'id':ident,'available':True,'helper_state':'exited','instances':[{'pid':1}]},
            {'id':ident,'available':False,'helper_state':'exited','instances':[]}):
            with patch.object(ppt.executor,'guest_call',return_value=response):
                with self.assertRaises(RuntimeError):ppt.recover(ident,self.policy)
            self.assertTrue(ppt.execution_status()['blocked'])
        with patch.object(ppt.executor,'guest_call',return_value={'id':ident,'available':True,'helper_state':'exited','instances':[]}):
            result=ppt.recover(ident,self.policy)
        self.assertFalse(result['blocked']);self.assertFalse(result['native_success'])
    def test_unclaimed_recovery_atomically_cancels_late_worker(self):
        with patch.object(ppt.executor,'guest_call',side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):ppt.embed(self.project,self.root/'first',policy=self.policy)
        ident=ppt.execution_status()['operation_id']
        result=ppt.recover(ident,self.policy)
        self.assertEqual(result['state'],'cancelled_before_dispatch');self.assertFalse(result['blocked'])

if __name__=='__main__':unittest.main()
