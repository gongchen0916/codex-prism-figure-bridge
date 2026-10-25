import hashlib,json,sys,tempfile,unittest,uuid
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

    def fast_job(self):
        def activated(policy,action,job,**kwargs):
            q=json.loads((job/'ppt-request.json').read_text());(job/'ppt.claim').write_text('123')
            result={'id':q['id'],'source_sha256':q['source_sha256'],'state':'activated_awaiting_edit_verification'}
            (job/'ppt-receipt.json').write_text(json.dumps(result));return result
        with patch.object(ppt.executor,'guest_call',side_effect=activated):ppt.embed(self.project,self.root/'fast',activate=True,policy=self.policy)
        job=Path(json.loads((self.root/'fast/windows-ppt-job.json').read_text())['job']);q=json.loads((job/'ppt-request.json').read_text())
        sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
        (job/'fast-finish-request.json').write_text(json.dumps({'schema':1,'id':q['id'],'source_sha256':q['source_sha256'],
            'ppt_request_sha256':sha(job/'ppt-request.json'),'ppt_receipt_sha256':sha(job/'ppt-receipt.json')}))
        (job/'ppt-session-closed.json').write_text(json.dumps({'id':q['id'],'closed':True}));return job

    def test_combined_finish_stays_fenced_after_parent_close(self):
        job=self.fast_job();self.assertTrue(ppt.execution_status()['blocked'],'Closed parent is not completed combined cleanup/reopen')
        for name in('fast-finish','fast-accept'):(job/(name+'.claim')).write_text(json.dumps({'id':job.name,'pid':123}))
        (job/'fast-finish-result.json').write_text(json.dumps({'id':job.name,'status':'completed'}))
        self.assertTrue(ppt.execution_status()['blocked'])
        acceptance={'id':job.name,'fresh_readonly_reopen':True,'prior_actual_DoVerb_verified':True}
        (job/'closed-native-acceptance.json').write_text(json.dumps(acceptance))
        (job/'fast-accept-result.json').write_text(json.dumps({'id':job.name,'status':'completed','acceptance':acceptance,'empty_office':{'status':'empty_PowerPoint_normally_closed'}}))
        self.assertFalse(ppt.execution_status()['blocked'])

    def test_recovery_checks_finalizer_liveness_and_cancels_unclaimed_stages(self):
        job=self.fast_job();(job/'fast-finish.claim').write_text(json.dumps({'id':job.name,'pid':456}))
        def observed(policy,action,path,**kwargs):
            if action=='ppt_status':return {'id':job.name,'available':True,'helper_state':'exited','instances':[]}
            return {'id':job.name,'finish_helper_state':'alive','accept_helper_state':'not_claimed'}
        with patch.object(ppt.executor,'guest_call',side_effect=observed),self.assertRaises(RuntimeError):ppt.recover(job.name,self.policy)
        self.assertTrue(ppt.execution_status()['blocked'])
        def exited(policy,action,path,**kwargs):
            return {'id':job.name,'available':True,'helper_state':'exited','instances':[],'finish_helper_state':'exited','accept_helper_state':'not_claimed'}
        with patch.object(ppt.executor,'guest_call',side_effect=exited):result=ppt.recover(job.name,self.policy)
        self.assertEqual(result['state'],'interrupted_process_exit');self.assertTrue(json.loads((job/'fast-accept.claim').read_text())['cancelled_before_dispatch'])

    def test_late_finalizer_claim_cannot_race_terminal_recovery(self):
        job=self.fast_job();(job/'fast-finish.claim').write_text(json.dumps({'id':job.name,'pid':456}))
        def race(policy,action,path,**kwargs):
            if action=='ppt_status':return {'id':job.name,'available':True,'helper_state':'exited','instances':[]}
            (job/'fast-accept.claim').write_text(json.dumps({'id':job.name,'pid':789}))
            return {'id':job.name,'finish_helper_state':'exited','accept_helper_state':'not_claimed'}
        with patch.object(ppt.executor,'guest_call',side_effect=race),self.assertRaises(RuntimeError):ppt.recover(job.name,self.policy)
        self.assertTrue(ppt.execution_status()['blocked'])

    def test_corrupt_finish_intent_fails_closed(self):
        job=self.fast_job();(job/'fast-finish-request.json').write_text('[]')
        try:status=ppt.execution_status()
        except Exception as error:self.fail('Corrupt stage must return a blocked status, not escape: '+str(error))
        self.assertEqual(status['state'],'invalid_journal');self.assertTrue(status['blocked'])

if __name__=='__main__':unittest.main()
