"""Protocol and durable-journal fault injection; never launch native applications."""
import json
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import prism_mcp_server as server


class ErrorReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bridge = server.bridge
        self.patch(self.bridge, 'PRISM_LOCK_PATH', self.root / '.lock')
        self.patch(self.bridge, 'execution_target', return_value={'ready': True, 'backend': 'windows_vm'})
        self.patch(self.bridge, 'prism_process_inventory', return_value={'available': True, 'instances': [{'pid': 4242, 'started': 'fixture'}]})
        self.patch(self.bridge.windows_prism_executor, 'refresh', return_value=None)
        self.patch(self.bridge.windows_prism_executor, 'cancel_not_started', return_value=False)
        # RuntimeState owns a separate module instance; keep the PPT journal isolated too.
        self.patch(self.bridge.windows_prism_ppt, '_journal', return_value=self.root / '.ppt.json')
        self.script = self.root / 'one_export.pzc'
        self.script.write_text('CreateLog\n')
        self.worker = self.patch(self.bridge, '_run_prism_script_unchecked', side_effect=BrokenPipeError('worker pipe closed'))

    def patch(self, *args, **kwargs):
        p = patch.object(*args, **kwargs)
        value = p.start()
        self.addCleanup(p.stop)
        return value

    def request(self, name, args=None):
        return server.handle({'jsonrpc': '2.0', 'id': 12, 'method': 'tools/call',
                              'params': {'name': name, 'arguments': {} if args is None else args}})

    def receipt(self, response):
        content = response['result']['content']
        self.assertEqual(content[0]['type'], 'text')
        try:
            value = json.loads(content[0]['text'])
        except json.JSONDecodeError:
            self.fail('Tool failures must return a JSON error receipt, not plain text')
        required = {'error_code', 'operation_id', 'execution_state', 'retryable', 'next_action'}
        self.assertTrue(required <= set(value), 'Missing structured error receipt fields')
        self.assertIs(type(value['retryable']), bool)
        return value

    def install_native_draw(self):
        def draw(args):
            ok, message = self.bridge.run_prism_script(self.script, timeout=0)
            return server.text_content(json.dumps({'ok': ok, 'prism_run_ok': ok, 'prism_log': message}))
        self.patch(server, 'tool_prism_draw', side_effect=draw)

    def draw(self):
        return self.request('prism_draw', {'data': 'A\n1\n', 'hint': 'test'})

    def catalog(self):
        return server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})['result']['tools']

    def test_environment_unavailable_has_receipt_and_stable_catalog(self):
        initial = self.catalog()
        self.patch(self.bridge, 'execution_target', return_value={'ready': False, 'reason': 'Not configured'})
        result = self.draw()
        self.assertTrue(result['result']['isError'])
        receipt = self.receipt(result)
        self.assertEqual(receipt['error_code'], 'ENVIRONMENT_NOT_READY')
        self.assertEqual(receipt['execution_state'], 'not_started')
        self.assertIsNone(receipt['operation_id'])
        self.assertFalse(receipt['retryable'])
        self.worker.assert_not_called()
        self.assertEqual(self.catalog(), initial)

    def test_argument_rejection_is_proven_not_started(self):
        receipt = self.receipt(self.request('prism_draw', {'hint': 'missing data'}))
        self.assertEqual(receipt['error_code'], 'INVALID_ARGUMENTS')
        self.assertEqual(receipt['execution_state'], 'not_started')
        self.assertFalse(receipt['retryable'])
        self.worker.assert_not_called()

    def test_malformed_params_stay_inside_tool_error_boundary(self):
        response = server.handle({'id': 1, 'method': 'tools/call', 'params': ['bad']})
        self.assertEqual(self.receipt(response)['error_code'], 'INVALID_ARGUMENTS')

    def test_worker_disconnect_leaves_queryable_fence_and_no_retry(self):
        self.install_native_draw()
        catalog = self.catalog()
        result = self.draw()
        receipt = self.receipt(result)
        self.assertTrue(result['result']['isError'])
        self.assertEqual(receipt['error_code'], 'EXECUTION_OUTCOME_UNKNOWN')
        self.assertEqual(receipt['execution_state'], 'unknown')
        self.assertFalse(receipt['retryable'])
        self.assertEqual(receipt['operation_id'], self.bridge.execution_status(passive=True)['operation_id'])
        self.assertIn('prism_recover_execution', receipt['next_action'])
        environment = self.request('prism_env', {'runtime_only': True})
        self.assertFalse(environment['result']['isError'])
        self.assertEqual(json.loads(environment['result']['content'][0]['text'])['execution']['state'], 'unknown')
        self.draw()
        self.assertEqual(self.worker.call_count, 1)
        self.assertEqual(self.catalog(), catalog)

    def test_late_completion_receipt_does_not_repeat_native_action(self):
        self.install_native_draw()
        self.draw()
        self.script.with_suffix('.log').write_text('COMPLETE! No Errors.\n')
        (self.root / 'done.txt').write_text('done')
        (self.root / 'one.svg').write_text('<svg/>')
        response = self.draw()
        # Preserve legacy returned-summary semantics, while adding a receipt.
        self.assertFalse(response['result']['isError'])
        receipt = self.receipt(response)
        self.assertFalse(receipt['ok'])
        self.assertEqual(receipt['error_code'], 'EXECUTION_COMPLETED_LATE')
        self.assertEqual(receipt['execution_state'], 'completed_late')
        self.assertFalse(receipt['retryable'])
        self.assertEqual(self.worker.call_count, 1)
        self.assertEqual(self.bridge.execution_status(passive=True)['state'], 'completed_late')

    def test_unjournaled_child_timeout_never_claims_not_started(self):
        self.patch(server, 'tool_windows_library', side_effect=subprocess.TimeoutExpired('worker', 1))
        receipt = self.receipt(self.request('prism_windows_run', {'request': 'fixture.json', 'output': 'output'}))
        self.assertEqual(receipt['execution_state'], 'unknown')
        self.assertIsNone(receipt['operation_id'])
        self.assertFalse(receipt['retryable'])

    def test_unrelated_previous_completed_operation_is_not_attached_to_failure(self):
        record = self.bridge.execution_state.begin(self.bridge._execution_journal(), self.script, self.root / 'done.txt', None, [])
        self.bridge.execution_state.save(self.bridge._execution_journal(), record, 'completed')
        self.patch(server, 'tool_windows_library', side_effect=BrokenPipeError('lost worker'))
        receipt = self.receipt(self.request('prism_windows_run', {'request': 'fixture.json', 'output': 'output'}))
        self.assertIsNone(receipt['operation_id'])
        self.assertEqual(receipt['execution_state'], 'unknown')
        self.assertFalse(receipt['retryable'])

    def test_read_only_timeout_can_retry_without_replaying_native_actions(self):
        self.patch(server, 'tool_windows_library', side_effect=subprocess.TimeoutExpired('catalog', 1))
        receipt = self.receipt(self.request('prism_windows_catalog'))
        self.assertEqual(receipt['error_code'], 'OBSERVATION_UNAVAILABLE')
        self.assertEqual(receipt['execution_state'], 'not_started')
        self.assertTrue(receipt['retryable'])

    def test_corrupt_journal_never_claims_safe_retry(self):
        self.bridge._execution_journal().write_text('{broken')
        self.install_native_draw()
        receipt = self.receipt(self.draw())
        self.assertEqual(receipt['error_code'], 'EXECUTION_STATE_UNAVAILABLE')
        self.assertEqual(receipt['execution_state'], 'unknown')
        self.assertFalse(receipt['retryable'])
        self.worker.assert_not_called()

    def test_stale_runtime_retains_diagnostics_and_fixed_catalog(self):
        catalog = self.catalog()
        status = {'error': 'runtime_restart_required', 'restart_required': True, 'changed_files': ['scripts/fixture.py']}
        self.patch(server.RUNTIME_STATE, 'assert_current', side_effect=server.RuntimeRestartRequired(status))
        receipt = self.receipt(self.draw())
        self.assertEqual(receipt['error_code'], 'RUNTIME_RESTART_REQUIRED')
        self.assertTrue(receipt['restart_required'])
        self.assertEqual(receipt['execution_state'], 'not_started')
        self.assertEqual(self.catalog(), catalog)
        self.assertFalse(self.request('prism_env', {'runtime_only': True})['result']['isError'])

    def test_success_and_unverified_preview_are_unchanged(self):
        for content in (server.text_content('{"ok":true,"paths":{}}'),
                        server.text_content('{"ok":false,"render_ok":true,"safe_for_publication":false}')):
            with patch.object(server, 'tool_prism_draw', return_value=content):
                result = self.draw()['result']
            self.assertEqual(result, {'content': content, 'isError': False})

    def test_windows_run_environment_failure_is_proven_not_started(self):
        self.patch(self.bridge, 'execution_target', return_value={'ready': False, 'reason': 'Not configured'})
        handler = self.patch(server, 'tool_windows_library', side_effect=AssertionError('Must not dispatch'))
        receipt = self.receipt(self.request('prism_windows_run', {'request': 'fixture.json', 'output': 'output'}))
        self.assertEqual(receipt['error_code'], 'ENVIRONMENT_NOT_READY')
        self.assertEqual(receipt['execution_state'], 'not_started')
        handler.assert_not_called()

    def test_early_predispatch_error_without_a_journal_stays_conservative(self):
        self.patch(server, 'tool_prism_draw', side_effect=ValueError('Generic error could happen after dispatch'))
        receipt = self.receipt(self.draw())
        self.assertEqual(receipt['execution_state'], 'unknown')
        self.assertFalse(receipt['retryable'])

    def test_failure_of_the_passive_inspector_does_not_escape_error_boundary(self):
        self.patch(self.bridge, 'execution_status', side_effect=OSError('Unreadable journal'))
        self.patch(server, 'tool_prism_draw', side_effect=BrokenPipeError('worker lost'))
        receipt = self.receipt(self.draw())
        self.assertEqual(receipt['error_code'], 'EXECUTION_STATE_UNAVAILABLE')
        self.assertFalse(receipt['retryable'])

    def test_ppt_unknown_has_priority_over_a_completed_native_script(self):
        self.patch(server, 'tool_prism_draw', side_effect=BrokenPipeError('ppt worker lost'))
        native = {'state': 'completed', 'blocked': False, 'operation_id': 'native-fixture'}
        ppt = {'state': 'unknown', 'blocked': True, 'operation_id': 'ppt-fixture'}
        self.patch(self.bridge, 'execution_status', return_value=native)
        self.patch(self.bridge.windows_prism_ppt, 'execution_status', return_value=ppt)
        receipt = self.receipt(self.draw())
        self.assertEqual(receipt['operation_id'], 'ppt-fixture')
        self.assertEqual(receipt['operation_scope'], 'observed_shared_queue')
        self.assertEqual(receipt['execution_state'], 'unknown')
        self.assertFalse(receipt['retryable'])

    def test_stdio_parent_survives_an_actual_worker_process_exit(self):
        isolated = self.root / 'isolated'
        shutil.copytree(ROOT / 'scripts', isolated / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
        driver = isolated / 'driver.py'
        driver.write_text(r'''
import json, pathlib, subprocess, sys
root = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(root / 'scripts'))
import prism_mcp_server as server
b = server.bridge
b.PRISM_LOCK_PATH = root / '.lock'
b.windows_prism_ppt._journal = lambda: root / '.ppt.json'
b.execution_target = lambda: {'ready': True, 'backend': 'windows_vm'}
b.prism_process_inventory = lambda: {'available': True, 'instances': [{'pid': 4242, 'started': 'fixture'}]}
script = root / 'one_export.pzc'
script.write_text('CreateLog\n')
def worker(*args):
    subprocess.run([sys.executable, '-c', 'import os; os._exit(37)'], check=True)
b._run_prism_script_unchecked = worker
def draw(args):
    ok, log = b.run_prism_script(script, timeout=0)
    return server.text_content(json.dumps({'ok': ok, 'prism_run_ok': ok, 'prism_log': log}))
server.tool_prism_draw = draw
raise SystemExit(server.main())
''')
        requests = [
            {'id': 1, 'method': 'tools/list'},
            {'id': 2, 'method': 'tools/call', 'params': {'name': 'prism_draw', 'arguments': {'data': 'A\n1', 'hint': 'test'}}},
            {'id': 3, 'method': 'tools/call', 'params': {'name': 'prism_env', 'arguments': {'runtime_only': True}}},
            {'id': 4, 'method': 'tools/list'},
        ]
        child = subprocess.run([sys.executable, '-B', str(driver)], input=''.join(json.dumps(r) + '\n' for r in requests),
                               text=True, capture_output=True, timeout=15, check=True)
        responses = [json.loads(line) for line in child.stdout.splitlines()]
        self.assertEqual([r['id'] for r in responses], [1, 2, 3, 4])
        self.assertEqual(self.receipt(responses[1])['error_code'], 'EXECUTION_OUTCOME_UNKNOWN')
        env = json.loads(responses[2]['result']['content'][0]['text'])
        self.assertFalse(responses[2]['result']['isError'])
        self.assertEqual(env['execution']['state'], 'unknown')
        self.assertEqual(responses[0]['result'], responses[3]['result'])
