import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
runtime = __import__('runtime_state') if importlib.util.find_spec('runtime_state') else None


class RuntimeStateTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(runtime, 'Filesystem runtime identity guard is missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'scripts').mkdir()
        self.code = self.root / 'scripts/logic.py'
        self.code.write_text('VALUE = 1\n')
        self.state = runtime.RuntimeState(self.root, version='test')
        self.state.finish_loading({})

    def test_unchanged_runtime_is_ready_without_external_calls(self):
        status = self.state.status()
        self.assertFalse(status['restart_required'])
        self.assertEqual(status['loaded_identity'], status['disk_identity'])
        self.assertEqual('test', status['version'])
        self.state.assert_current()

    def test_same_size_same_timestamp_code_edit_is_detected(self):
        before = self.code.stat()
        self.code.write_text('VALUE = 2\n')
        os.utime(self.code, ns=(before.st_atime_ns, before.st_mtime_ns))
        status = self.state.status()
        self.assertTrue(status['restart_required'])
        self.assertEqual(['scripts/logic.py'], status['changed_files'])
        with self.assertRaisesRegex(runtime.RuntimeRestartRequired, 'restart|reconnect'):
            self.state.assert_current()

    def test_metadata_change_is_not_mistaken_for_loaded_code_change(self):
        assets = self.root / 'assets/templates'
        assets.mkdir(parents=True)
        (assets / 'template_index.json').write_text('{"templates":[]}')
        self.assertFalse(self.state.status()['restart_required'])

    def test_windows_library_code_is_bound_but_run_evidence_is_not(self):
        library = self.root / 'development/library_v1'
        library.mkdir(parents=True)
        helper = library / 'geometry.py'
        helper.write_text('VALUE = 1\n')
        state = runtime.RuntimeState(self.root, version='test')
        state.finish_loading({})
        evidence = library / 'runs'
        evidence.mkdir()
        (evidence / 'result.json').write_text('{}')
        self.assertFalse(state.status()['restart_required'])
        helper.write_text('VALUE = 2\n')
        self.assertEqual(state.status()['changed_files'], ['development/library_v1/geometry.py'])
        with self.assertRaises(runtime.RuntimeRestartRequired):
            state.assert_current()

    def test_windows_library_symlink_is_rejected(self):
        library = self.root / 'development/library_v1'
        library.mkdir(parents=True)
        (library / 'geometry.py').symlink_to(self.code)
        state = runtime.RuntimeState(self.root, version='test')
        self.assertTrue(state.status()['restart_required'])

    def test_copied_code_has_path_independent_identity(self):
        other = self.root / 'copy'
        shutil.copytree(self.root / 'scripts', other / 'scripts')
        second = runtime.RuntimeState(other, version='test')
        second.finish_loading({})
        self.assertEqual(self.state.status()['loaded_identity'], second.status()['loaded_identity'])

    def test_change_during_startup_remains_blocked_after_bytes_restored(self):
        original = self.code.read_bytes()
        state = runtime.RuntimeState(self.root, version='test')
        self.code.write_text('VALUE = 2\n')
        state.finish_loading({})
        self.code.write_bytes(original)
        self.assertTrue(state.status()['restart_required'])

    def test_module_from_another_install_is_reported(self):
        foreign = types.SimpleNamespace(__file__=str(self.root / 'other/logic.py'))
        state = runtime.RuntimeState(self.root, version='test')
        state.finish_loading({'logic': foreign})
        self.assertTrue(state.status()['restart_required'])
        self.assertIn('logic', state.status()['module_root_mismatches'])

    def test_lazy_source_changed_after_startup_is_blocked_before_execution(self):
        sentinel = self.root / 'executed'
        self.code.write_text('from pathlib import Path\nPath(' + repr(str(sentinel)) + ').touch()\n')
        with self.assertRaises(runtime.RuntimeRestartRequired):
            self.state.load_module('logic')
        self.assertFalse(sentinel.exists())

    def test_lazy_source_identity_records_compiled_bytes_and_never_hot_reloads(self):
        module = self.state.load_module('logic')
        self.assertEqual(1, module.VALUE)
        status = self.state.status()
        self.assertEqual(runtime.hashlib.sha256(self.code.read_bytes()).hexdigest(),
                         status['loaded_source_hashes']['scripts/logic.py'])
        self.code.write_text('VALUE = 2\n')
        self.assertIs(module, self.state.load_module('logic'))
        self.assertEqual(1, module.VALUE)
        self.assertTrue(self.state.status()['restart_required'])


class RuntimeMcpIntegrationTests(unittest.TestCase):
    def run_copied_server(self, program):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / 'scripts', root / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
            cp = subprocess.run([sys.executable, '-I', '-c', program, str(root)],
                                capture_output=True, text=True, timeout=15)
            self.assertEqual(0, cp.returncode, cp.stderr)
            return json.loads(cp.stdout)

    def test_foreign_transitive_and_lazy_modules_cannot_enter_runtime(self):
        result = self.run_copied_server('''import json, pathlib, sys, types
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / 'scripts'))
for name in ('template_matcher', 'published_templates'):
    module = types.ModuleType(name)
    module.__file__ = '/foreign-install/' + name + '.py'
    sys.modules[name] = module
import prism_mcp_server as s
# The same resolver used by lazy imports in server functions must stay private.
namespace = s.published_entry.__globals__['__builtins__']
loader = namespace['__import__'] if isinstance(namespace, dict) else namespace.__import__
lazy = loader('published_templates')
print(json.dumps({'matcher': s.bridge.template_matcher.__file__,
                  'lazy': lazy.__file__, 'root': str(root),
                  'foreign_preserved': sys.modules['template_matcher'].__file__,
                  'status': s.RUNTIME_STATE.status()}))
''')
        self.assertEqual(result['root'] + '/scripts/template_matcher.py', result['matcher'])
        self.assertEqual(result['root'] + '/scripts/published_templates.py', result['lazy'])
        self.assertEqual('/foreign-install/template_matcher.py', result['foreign_preserved'])
        self.assertFalse(result['status']['restart_required'])

    def test_stale_valid_bytecode_cannot_supply_local_execution(self):
        result = self.run_copied_server('''import json, os, pathlib, py_compile, sys
root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root / 'scripts'))
for name in ('runtime_state', 'prism_bridge', 'template_matcher', 'template_identity', 'published_templates'):
    path = root / 'scripts' / (name + '.py')
    source = path.read_bytes() + b"\\nPYCTAG = 'OLD'\\n"
    path.write_bytes(source)
    before = path.stat()
    py_compile.compile(str(path), doraise=True)
    path.write_bytes(source.replace(b"PYCTAG = 'OLD'", b"PYCTAG = 'NEW'"))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
import prism_mcp_server as s
namespace = s.published_entry.__globals__['__builtins__']
loader = namespace['__import__'] if isinstance(namespace, dict) else namespace.__import__
lazy = loader('published_templates')
print(json.dumps({'bridge': s.bridge.PYCTAG, 'matcher': s.bridge.template_matcher.PYCTAG,
                  'lazy': lazy.PYCTAG, 'status': s.RUNTIME_STATE.status()}))
''')
        self.assertEqual(('NEW', 'NEW', 'NEW'), (result['bridge'], result['matcher'], result['lazy']))
        self.assertFalse(result['status']['restart_required'])

    def test_stale_server_bytecode_is_reported_as_restart_required(self):
        result = self.run_copied_server('''import json, os, pathlib, py_compile, subprocess, sys
root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root / 'scripts'))
path = root / 'scripts/prism_mcp_server.py'
source = path.read_bytes() + b"\\nPYCTAG = 'OLD'\\n"
path.write_bytes(source)
before = path.stat()
py_compile.compile(str(path), doraise=True)
path.write_bytes(source.replace(b"PYCTAG = 'OLD'", b"PYCTAG = 'NEW'"))
os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
import prism_mcp_server as s
request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
           'params': {'name': 'prism_env', 'arguments': {'runtime_only': True}}}
reconnected = subprocess.run([sys.executable, str(path)], input=json.dumps(request) + '\\n',
                             capture_output=True, text=True, env={**os.environ, 'TMPDIR': str(root)})
print(json.dumps({'tag': s.PYCTAG, 'status': s.RUNTIME_STATE.status(),
                  'reconnected': json.loads(reconnected.stdout)}))
''')
        self.assertEqual('OLD', result['tag'])
        self.assertTrue(result['status']['restart_required'])
        self.assertFalse(result['reconnected']['result']['isError'])
        fresh = json.loads(result['reconnected']['result']['content'][0]['text'])
        self.assertFalse(fresh['runtime']['restart_required'])

    def test_child_runner_uses_source_and_preserves_arguments(self):
        result = self.run_copied_server('''import json, os, pathlib, py_compile, subprocess, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / 'scripts'))
path = root / 'scripts/template_identity.py'
source = path.read_bytes() + b"\\nPYCTAG = 'OLD'\\n"
path.write_bytes(source)
before = path.stat()
py_compile.compile(str(path), doraise=True)
path.write_bytes(source.replace(b"PYCTAG = 'OLD'", b"PYCTAG = 'NEW'"))
os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
(root / 'scripts/probe.py').write_text("import json, template_identity\\ndef main(argv):\\n print(json.dumps({'tag': template_identity.PYCTAG, 'args': argv}))\\n return 0\\n")
import prism_mcp_server as s
cp = subprocess.run([sys.executable, str(root / 'scripts/runtime_state.py'),
                     '--root', str(root), '--expected-identity', s.RUNTIME_STATE.status()['loaded_identity'],
                     '--execute', 'probe', '--example', 'two words'], capture_output=True, text=True)
print(json.dumps({'code': cp.returncode, 'stdout': cp.stdout, 'stderr': cp.stderr}))
''')
        self.assertEqual(0, result['code'], result['stderr'])
        self.assertEqual({'tag': 'NEW', 'args': ['--example', 'two words']}, json.loads(result['stdout']))

    def test_child_runner_refuses_changed_release_before_entrypoint(self):
        result = self.run_copied_server('''import json, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / 'scripts'))
sentinel = root / 'executed'
path = root / 'scripts/probe.py'
path.write_text("from pathlib import Path\\nPath(" + repr(str(sentinel)) + ").touch()\\ndef main(argv): return 0\\n")
import prism_mcp_server as s
expected = s.RUNTIME_STATE.status()['loaded_identity']
path.write_text(path.read_text() + '# release changed\\n')
cp = subprocess.run([sys.executable, str(root / 'scripts/runtime_state.py'),
                     '--root', str(root), '--expected-identity', expected,
                     '--execute', 'probe'], capture_output=True, text=True)
print(json.dumps({'code': cp.returncode, 'stderr': cp.stderr, 'executed': sentinel.exists()}))
''')
        self.assertNotEqual(0, result['code'])
        self.assertRegex(result['stderr'].lower(), 'restart|reconnect')
        self.assertFalse(result['executed'])

    def test_old_process_rejects_changed_code_before_input_or_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / 'scripts', root / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
            program = '''import json, pathlib, sys
sys.path.insert(0, sys.argv[1] + '/scripts')
import prism_mcp_server as s
calls = []
s.published_entry = lambda *_: None
def trap(*_):
    calls.append('input_or_output')
    raise RuntimeError('unguarded handler reached')
s.resolve_data_path = trap
path = pathlib.Path(s.__file__)
path.write_text(path.read_text() + '\\n# changed after this process loaded\\n')
try:
    s.call_tool('prism_draw', {'hint':'watercolor-box','data':'A,B\\n1,2\\n2,3','run_prism':False})
    message = 'no error'
except Exception as exc:
    message = str(exc)
print(json.dumps({'calls':calls,'message':message}))
'''
            cp = subprocess.run([sys.executable, '-I', '-c', program, str(root)], capture_output=True, text=True, timeout=15)
            self.assertEqual(0, cp.returncode, cp.stderr)
            result = json.loads(cp.stdout)
            self.assertEqual([], result['calls'])
            self.assertRegex(result['message'].lower(), 'restart|reconnect')

    def test_runtime_only_environment_does_not_query_prism(self):
        import prism_mcp_server as server
        from unittest.mock import patch
        with patch.object(server, 'run_bridge_cli', side_effect=AssertionError('Prism process must not be queried')):
            try:
                response = server.call_tool('prism_env', {'runtime_only': True})
            except Exception as exc:
                self.fail('Filesystem-only runtime status unavailable: ' + str(exc))
        result = json.loads(response[0]['text'])
        self.assertFalse(result['prism_checked'])
        self.assertIn('restart_required', result['runtime'])


if __name__ == '__main__':
    unittest.main()
