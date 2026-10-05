"""Windows migration must never start the formerly configured Mac app."""
import contextlib,io,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import prism_bridge as bridge
import prism_mcp_server as server
import runtime_state

class WindowsOnlyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        p=patch.object(bridge,'PRISM_LOCK_PATH',Path(self.temp.name)/'.lock');p.start();self.addCleanup(p.stop)
        root=Path(self.temp.name);(root/'assets').mkdir()
        (root/'assets/prism_execution.json').write_text(json.dumps({'schema':1,'backend':'windows_vm','vm_id':'{11111111-1111-4111-8111-111111111111}','vm_name':'Windows 11','phase':'awaiting_windows_installation_and_acceptance','macos_execution_allowed':False}))
        p=patch.object(bridge,'SKILL_ROOT',root);p.start();self.addCleanup(p.stop)
        p=patch.object(server.bridge,'SKILL_ROOT',root);p.start();self.addCleanup(p.stop)
    def test_native_dispatch_is_blocked_even_if_mac_prism_is_installed(self):
        with tempfile.TemporaryDirectory()as d:
            root=Path(d);script=root/'probe.pzc';script.write_text('CreateLog\n')
            with patch.object(bridge,'DEFAULT_PRISM_APP',root),patch.object(bridge.platform,'system',return_value='Darwin'),patch.object(bridge.subprocess,'run',side_effect=AssertionError('Mac launch forbidden')):
                ok,message=bridge.run_prism_script(script,timeout=0,done_name='probe_done.txt')
            self.assertFalse(ok);self.assertIn('Windows',message);self.assertIn('pending',message)
    def test_staged_export_blocks_before_creating_a_stage_or_probe(self):
        with tempfile.TemporaryDirectory()as d:
            root=Path(d);project=root/'source.pzfx';project.write_text('<PrismFile/>');out=root/'output';out.mkdir()
            with patch.object(bridge,'STAGING_ROOT',root/'stages'),patch.object(bridge,'run_prism_probe',side_effect=AssertionError('No probe before Windows readiness')):
                ok,message,svg=bridge.run_prism_export_staged(project,out,'case',timeout=1)
            self.assertFalse(ok);self.assertIn('Windows',message);self.assertFalse((root/'stages').exists())
    def test_full_environment_identifies_windows_and_does_not_query_mac_app(self):
        with patch.object(server,'run_bridge_cli',side_effect=AssertionError('Mac environment must not be queried')):
            result=json.loads(server.tool_prism_env({})[0]['text'])
        self.assertIn('execution_target',result);target=result['execution_target'];self.assertEqual(target['backend'],'windows_vm');self.assertFalse(target['macos_execution_allowed']);self.assertFalse(target['ready'])
        self.assertNotIn('prism_application',result)
    def test_missing_or_invalid_policy_does_not_enable_mac(self):
        self.assertTrue(hasattr(bridge,'execution_target'),'Execution policy missing')
        with tempfile.TemporaryDirectory()as d,patch.object(bridge,'SKILL_ROOT',Path(d)):
            target=bridge.execution_target();self.assertFalse(target['macos_execution_allowed']);self.assertFalse(target['ready'])
            p=Path(d)/'assets/prism_execution.json';p.parent.mkdir();p.write_text('{"backend":"macos"}')
            self.assertFalse(bridge.execution_target()['ready']);self.assertFalse(bridge.execution_target()['macos_execution_allowed'])
    def test_policy_edit_requires_reconnection(self):
        with tempfile.TemporaryDirectory()as d:
            root=Path(d);(root/'scripts').mkdir();(root/'scripts/a.py').write_text('VALUE=1\n');p=root/'assets/prism_execution.json';p.parent.mkdir();p.write_text('{}')
            state=runtime_state.RuntimeState(root,version='test');state.finish_loading({});p.write_text('{"changed":true}')
            self.assertTrue(state.status()['restart_required']);self.assertIn('assets/prism_execution.json',state.status()['changed_files'])
    def test_mcp_drawing_fails_before_creating_any_user_output(self):
        with tempfile.TemporaryDirectory()as d:
            out=Path(d)/'must-not-exist'
            with self.assertRaisesRegex(Exception,'Windows'):
                server.call_tool('prism_draw',{'data':'A,B\n1,2\n2,3\n','hint':'散点图','outdir':str(out)})
            self.assertFalse(out.exists())
