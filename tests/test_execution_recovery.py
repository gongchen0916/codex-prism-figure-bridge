"""Exercise durable native-dispatch uncertainty without launching Prism."""
import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import prism_bridge as b
import runtime_state

class ExecutionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.script=self.root/'one_export.pzc';self.script.write_text('CreateLog\n');self.app=self.root/'Prism.app';self.app.mkdir()
        for target,name,value in((b,'STAGING_ROOT',self.root/'stages'),(b,'PRISM_LOCK_PATH',self.root/'stages/.lock'),(b,'DEFAULT_PRISM_APP',self.app),(b.platform,'system',lambda:'Darwin')):
            p=patch.object(target,name,value);p.start();self.addCleanup(p.stop)
        p=patch.object(b,'_require_native_execution',return_value={'backend':'windows_vm','ready':True});p.start();self.addCleanup(p.stop)
        # Public source tests must not depend on the developer's private VM policy.
        for target,name,value in ((b,'execution_target',{'backend':'windows_vm','ready':True}),(b.windows_prism_executor,'refresh',None),(b.windows_prism_executor,'cancel_not_started',False)):
            p=patch.object(target,name,return_value=value);p.start();self.addCleanup(p.stop)
        self.launch=patch.object(b,'_run_prism_script_unchecked',return_value=(False,'Timed out waiting for native export'));self.runner=self.launch.start();self.addCleanup(self.launch.stop)
        p=patch.object(b,'prism_process_inventory',return_value={'available':True,'instances':[{'pid':4242,'started':'fixture'}]},create=True);p.start();self.addCleanup(p.stop)
    def state(self):
        self.assertTrue(hasattr(b,'execution_status'),'Durable execution diagnostics missing')
        return b.execution_status()
    def complete(self):
        self.script.with_suffix('.log').write_text('COMPLETE! No Errors.\n');(self.root/'done.txt').write_text('done');(self.root/'one.svg').write_text('<svg/>')
    def test_timeout_does_not_allow_a_second_dispatch(self):
        self.assertFalse(b.run_prism_script(self.script,timeout=0)[0]);second=self.root/'two.pzc';second.write_text('CreateLog\n')
        self.assertFalse(b.run_prism_script(second,timeout=0,done_name='two_done.txt')[0]);self.assertEqual(self.runner.call_count,1,'A timed-out Prism operation must fence later launches')
    def test_wrong_reserved_export_marker_rejects_before_native_dispatch(self):
        script=self.root/'snapshot.pzc';script.write_text('CreateLog\n')
        with self.assertRaisesRegex(ValueError,'done.txt'):b.run_prism_script(script,timeout=1)
        self.runner.assert_not_called();self.assertEqual(self.state()['state'],'idle')
    def test_invalid_windows_file_reference_cannot_create_an_uncertain_operation(self):
        self.script.write_text('CreateLog\nOpen "/absolute/input.prism"\n')
        with self.assertRaisesRegex(ValueError,'local filenames'):
            b.run_prism_script(self.script,timeout=1)
        self.runner.assert_not_called();self.assertEqual(self.state()['state'],'idle')
    def test_legacy_preflight_failure_can_recover_without_stopping_prism(self):
        self.script.write_text('CreateLog\nSetPath "stage"\nOpen "/absolute/input.prism"\n')
        record=b.execution_state.begin(b._execution_journal(),self.script,self.root/'done.txt',self.root/'one.svg',[{'pid':4242,'started':'fixture'}])
        b.execution_state.save(b._execution_journal(),record,'unknown','Controller interrupted; native execution may still be running.')
        result=b.recover_execution(record['operation_id'])
        self.assertEqual(result['state'],'not_started');self.assertFalse(result['blocked'])
        self.assertFalse(result['native_completion_verified']);self.runner.assert_not_called()
    def test_preflight_proof_cannot_clear_changed_or_guest_staged_scripts(self):
        for staged in (False,True):
            self.script.write_text('CreateLog\nSetPath "stage"\nOpen "/absolute/input.prism"\n')
            record=b.execution_state.begin(b._execution_journal(),self.script,self.root/'done.txt',self.root/'one.svg',[{'pid':4242,'started':'fixture'}])
            b.execution_state.save(b._execution_journal(),record,'unknown','Controller interrupted; native execution may still be running.')
            if staged:self.script.with_suffix('.windows-job.json').write_text('{}')
            else:self.script.write_text(self.script.read_text()+'// changed\n')
            with patch.object(b.windows_prism_executor,'refresh'),patch.object(b.windows_prism_executor,'cancel_not_started',return_value=False),self.assertRaisesRegex(RuntimeError,'running'):
                b.recover_execution(record['operation_id'])
            self.assertTrue(b.execution_status(passive=True)['blocked'])
    def test_invalid_journal_blocks_dispatch_and_cleanup(self):
        b.PRISM_LOCK_PATH.parent.mkdir(parents=True);b.PRISM_LOCK_PATH.with_name('.lock.operation.json').write_text('{bad')
        self.assertFalse(b.run_prism_script(self.script,timeout=0)[0]);self.assertEqual(self.runner.call_count,0);self.assertEqual(self.state()['state'],'invalid_journal')
    def test_corrupt_freshness_signatures_cannot_relabel_old_outputs_as_new(self):
        self.complete();b.run_prism_script(self.script,timeout=0);path=b.PRISM_LOCK_PATH.with_name('.lock.operation.json');record=json.loads(path.read_text());record['before']={k:'bad'for k in('log','done','svg')};path.write_text(json.dumps(record))
        self.assertEqual(self.state()['state'],'invalid_journal');self.assertTrue(self.state()['blocked'])
    def test_launch_timeout_is_unknown_not_cancellation(self):
        self.runner.side_effect=subprocess.TimeoutExpired('open',1)
        ok,message=b.run_prism_script(self.script,timeout=1)
        self.assertFalse(ok);self.assertIn('operation_id=',message);self.assertTrue(self.state()['blocked'])
    def test_environment_does_not_send_apple_events(self):
        import contextlib,io
        with patch.object(b.platform,'platform',return_value='Darwin test'),patch.object(b.subprocess,'check_output',side_effect=AssertionError('Unexpected Apple event or command')),contextlib.redirect_stdout(io.StringIO())as output:b.environment()
        self.assertNotIn('Unexpected Apple event',output.getvalue())
    def test_cli_status_never_launches_prism(self):
        import contextlib,io
        with contextlib.redirect_stdout(io.StringIO())as output:self.assertEqual(b.main(['execution-status']),0)
        self.assertEqual(json.loads(output.getvalue())['state'],'idle');self.runner.assert_not_called()
    def test_unknown_state_survives_another_module_instance(self):
        b.run_prism_script(self.script,timeout=0);s=self.state();self.assertTrue(s['blocked']);self.assertEqual(s['state'],'unknown');self.assertTrue(s['operation_id'])
        import importlib.util
        spec=importlib.util.spec_from_file_location('other_bridge',b.__file__);other=importlib.util.module_from_spec(spec);spec.loader.exec_module(other);other.PRISM_LOCK_PATH=b.PRISM_LOCK_PATH
        self.assertEqual(other.execution_status()['operation_id'],s['operation_id'])
    def test_late_completion_is_detected_without_repeating_the_same_script(self):
        b.run_prism_script(self.script,timeout=0);self.complete()
        ok,message=b.run_prism_script(self.script,timeout=0);self.assertFalse(ok);self.assertEqual(self.runner.call_count,1);self.assertIn('completed_late',message);self.assertEqual(self.state()['state'],'completed_late')
    def test_new_operation_can_proceed_after_verified_late_completion(self):
        b.run_prism_script(self.script,timeout=0);self.complete();second=self.root/'two.pzc';second.write_text('CreateLog\n')
        b.run_prism_script(second,timeout=0,done_name='two_done.txt');self.assertEqual(self.runner.call_count,2)
    def test_stale_completion_and_modified_script_do_not_release_fence(self):
        self.complete();b.run_prism_script(self.script,timeout=0);self.script.write_text('CreateLog\nWText "modified"\n');self.complete()
        second=self.root/'two.pzc';second.write_text('CreateLog\n');b.run_prism_script(second,timeout=0,done_name='two_done.txt');self.assertEqual(self.runner.call_count,1)
    def test_process_recovery_requires_exact_operation_and_observed_exit(self):
        b.run_prism_script(self.script,timeout=0);s=self.state();self.assertTrue(hasattr(b,'recover_execution'))
        with self.assertRaises(ValueError):b.recover_execution('wrong')
        with self.assertRaisesRegex(RuntimeError,'running'):b.recover_execution(s['operation_id'])
        with patch.object(b,'prism_process_inventory',return_value={'available':True,'instances':[]}):
            result=b.recover_execution(s['operation_id'])
        self.assertFalse(result['blocked']);self.assertEqual(result['state'],'interrupted_process_exit');self.assertFalse(result['result_verified'])
    def test_unavailable_process_inventory_cannot_clear_an_operation(self):
        b.run_prism_script(self.script,timeout=0);s=self.state()
        with patch.object(b,'prism_process_inventory',return_value={'available':False,'instances':[]}):
            with self.assertRaises(RuntimeError):b.recover_execution(s['operation_id'])
    def test_success_does_not_leave_a_pending_execution(self):
        self.runner.side_effect=lambda *args,**kwargs:(self.complete()or (True,'COMPLETE! No Errors.'))
        self.assertTrue(b.run_prism_script(self.script,timeout=1)[0]);self.assertFalse(self.state()['blocked']);self.assertEqual(self.state()['state'],'completed')
    def test_interrupted_controller_leaves_a_fence(self):
        self.runner.side_effect=KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):b.run_prism_script(self.script,timeout=1)
        self.assertTrue(self.state()['blocked'])
    def test_export_lock_is_reentrant_for_existing_outer_workflows(self):
        with b.prism_export_lock(wait_timeout=.01):
            with b.prism_export_lock(wait_timeout=.01):pass
    def test_timeout_evidence_is_not_removed_by_cleanup(self):
        folder=b.STAGING_ROOT/'uncertain';folder.mkdir(parents=True);self.script=folder/'one_export.pzc';self.script.write_text('CreateLog\n');b.run_prism_script(self.script,timeout=0);os.utime(folder,(1,1));b.cleanup_stale_stages(max_entries=0,max_age_seconds=0);self.assertTrue(folder.exists())
    def test_old_uncertain_stage_survives_after_global_journal_advances(self):
        folder=b.STAGING_ROOT/'uncertain';folder.mkdir(parents=True);self.script=folder/'one_export.pzc';self.script.write_text('CreateLog\n');b.run_prism_script(self.script,timeout=0)
        current=b.PRISM_LOCK_PATH.with_name('.lock.operation.json');record=json.loads(current.read_text());record['state']='completed';current.write_text(json.dumps(record))
        os.utime(folder,(1,1));b.cleanup_stale_stages(max_entries=0,max_age_seconds=0);self.assertTrue(folder.exists(),'A later operation must not erase earlier uncertain evidence')

class InstanceDiagnosticsTests(unittest.TestCase):
    def test_instances_have_distinct_ids_and_explicit_stdio_transport(self):
        a=runtime_state.RuntimeState(ROOT,version='test');b_=runtime_state.RuntimeState(ROOT,version='test');s=a.status()
        self.assertIn('instance_id',s);self.assertNotEqual(s['instance_id'],b_.status()['instance_id']);self.assertEqual(s['instance_id'],a.status()['instance_id']);self.assertEqual(s['transport'],'stdio');self.assertIsNone(s['http_origin'])
