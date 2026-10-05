"""Source-only MCP startup and unavailable-environment behavior; no native apps."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SourcePackageTests(unittest.TestCase):
    def test_manifest_matches_distributed_runtime_bytes(self):
        manifest = json.loads((ROOT / 'MCP_SOURCE_MANIFEST.json').read_text())
        self.assertEqual(manifest['version'], '0.9.1')
        self.assertEqual(len({item['path'] for item in manifest['source_files']}),
                         len(manifest['source_files']))
        for item in manifest['source_files']:
            relative = Path(item['path'])
            self.assertEqual(relative.parent, Path('scripts'))
            self.assertEqual(relative.suffix, '.py')
            self.assertEqual(hashlib.sha256((ROOT / relative).read_bytes()).hexdigest(),
                             item['sha256'], str(relative))

    def test_missing_private_assets_preserve_protocol_and_tool_catalog(self):
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
             'params': {'name': 'prism_env', 'arguments': {'runtime_only': True}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
             'params': {'name': 'prism_list_palettes', 'arguments': {}}},
            {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 6, 'method': 'ping'},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / 'scripts', root / 'scripts',
                            ignore=shutil.ignore_patterns('__pycache__'))
            completed = subprocess.run(
                [sys.executable, '-B', str(root / 'scripts/prism_mcp_server.py')],
                input=''.join(json.dumps(item) + '\n' for item in requests),
                text=True, capture_output=True, timeout=15, cwd=root, check=True)
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual([item['id'] for item in responses], list(range(1, 7)))
        self.assertEqual(responses[0]['result']['serverInfo']['version'], '0.9.1')
        self.assertEqual(responses[1]['result']['tools'], responses[4]['result']['tools'])
        environment = json.loads(responses[2]['result']['content'][0]['text'])
        self.assertFalse(responses[2]['result']['isError'])
        self.assertFalse(environment['runtime']['restart_required'])
        self.assertEqual(environment['runtime']['loaded_identity'],
                         environment['runtime']['disk_identity'])
        self.assertEqual(environment['health']['layers']['configuration']['state'], 'unavailable')
        self.assertTrue(responses[3]['result']['isError'])
        self.assertEqual(responses[5]['result'], {})


if __name__ == '__main__':
    unittest.main()
