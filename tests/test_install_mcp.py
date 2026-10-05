import importlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
install_mcp = importlib.import_module("install_mcp")


def completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


class InstallMcpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "skill"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "scripts/prism_mcp_server.py").write_text("# server\n")
        self.codex = Path(self.tmp.name) / "bin/codex"
        self.python = Path(self.tmp.name) / "bin/python3"
        self.codex.parent.mkdir(parents=True)
        for path in (self.codex, self.python):
            path.write_text("#!/bin/sh\n")
            path.chmod(0o755)
        self.calls = []

    def registration(self, command=None, args=None):
        return {
            "name": "prism",
            "enabled": True,
            "transport": {
                "type": "stdio",
                "command": command or str(self.python),
                "args": args if args is not None else [str(self.root / "scripts/prism_mcp_server.py")],
                "env": {"SECRET_TOKEN": "do-not-log"},
                "env_vars": ["SECRET_TOKEN"],
                "cwd": None,
            },
            "tools": None,
        }

    def runner(self, args, **kwargs):
        args = [str(x) for x in args]
        self.calls.append(args)
        if args[1:5] == ["mcp", "get", "prism", "--json"]:
            return completed(args, stdout=json.dumps(self.registration()))
        if args[1:] == ["-c", install_mcp.PYTHON_VERSION_CODE]:
            return completed(args, stdout="3.9.18\n")
        raise AssertionError(args)

    def test_matching_existing_registration_is_left_unchanged(self):
        result = install_mcp.ensure_registration(
            self.root, codex_cli=self.codex, run=self.runner
        )
        self.assertEqual(result["status"], "unchanged")
        self.assertFalse(result["changed"])
        flattened = "\n".join(" ".join(call) for call in self.calls)
        self.assertNotIn("mcp remove", flattened)
        self.assertNotIn("mcp add", flattened)
        self.assertNotIn("do-not-log", json.dumps(result))

    def test_conflicting_registration_is_preserved_and_safely_reported(self):
        def runner(args, **kwargs):
            args = [str(x) for x in args]
            self.calls.append(args)
            if args[1:5] == ["mcp", "get", "prism", "--json"]:
                current = self.registration(args=["/different/server.py"])
                return completed(args, stdout=json.dumps(current))
            raise AssertionError(args)

        result = install_mcp.ensure_registration(
            self.root, interpreter=self.python, codex_cli=self.codex, run=runner
        )
        self.assertEqual(result["status"], "conflict")
        self.assertFalse(result["changed"])
        self.assertEqual(result["current"]["args"], ["/different/server.py"])
        flattened = "\n".join(" ".join(call) for call in self.calls)
        self.assertNotIn("mcp remove", flattened)
        self.assertNotIn("mcp add", flattened)
        self.assertNotIn("SECRET_TOKEN", json.dumps(result))

    def test_absent_registration_uses_documented_add_and_verifies(self):
        gets = 0

        def runner(args, **kwargs):
            nonlocal gets
            args = [str(x) for x in args]
            self.calls.append(args)
            if args[1:5] == ["mcp", "get", "prism", "--json"]:
                gets += 1
                if gets == 1:
                    return completed(args, 1, stderr="MCP server 'prism' not found")
                return completed(args, stdout=json.dumps(self.registration()))
            if args[0] == str(self.python) and args[1:] == ["-c", install_mcp.PYTHON_VERSION_CODE]:
                return completed(args, stdout="3.11.9\n")
            if args[:4] == [str(self.codex), "mcp", "add", "prism"]:
                return completed(args)
            raise AssertionError(args)

        result = install_mcp.ensure_registration(
            self.root, interpreter=self.python, codex_cli=self.codex, run=runner
        )
        self.assertEqual(result["status"], "added")
        self.assertEqual(
            self.calls[-2],
            [str(self.codex), "mcp", "add", "prism", "--", str(self.python),
             str(self.root / "scripts/prism_mcp_server.py")],
        )
        self.assertFalse(any("remove" in call for call in self.calls))

    def test_add_failure_is_reported_without_remove(self):
        def runner(args, **kwargs):
            args = [str(x) for x in args]
            self.calls.append(args)
            if args[1:5] == ["mcp", "get", "prism", "--json"]:
                return completed(args, 1, stderr="MCP server 'prism' not found")
            if args[0] == str(self.python):
                return completed(args, stdout="3.10.0\n")
            if args[:4] == [str(self.codex), "mcp", "add", "prism"]:
                return completed(args, 7, stderr="configuration write failed")
            raise AssertionError(args)

        with self.assertRaisesRegex(RuntimeError, "add.*failed"):
            install_mcp.ensure_registration(
                self.root, interpreter=self.python, codex_cli=self.codex, run=runner
            )
        self.assertFalse(any("remove" in call for call in self.calls))

    def test_python_older_than_39_is_rejected_before_add(self):
        def runner(args, **kwargs):
            args = [str(x) for x in args]
            self.calls.append(args)
            if args[1:5] == ["mcp", "get", "prism", "--json"]:
                return completed(args, 1, stderr="MCP server 'prism' not found")
            if args[0] == str(self.python):
                return completed(args, stdout="3.8.18\n")
            raise AssertionError(args)

        with self.assertRaisesRegex(RuntimeError, "3.9"):
            install_mcp.ensure_registration(
                self.root, interpreter=self.python, codex_cli=self.codex, run=runner
            )
        self.assertFalse(any("add" in call for call in self.calls))

    def test_unrelated_not_found_error_is_not_treated_as_absent_registration(self):
        def runner(args, **kwargs):
            args = [str(x) for x in args]
            self.calls.append(args)
            if args[1:5] == ["mcp", "get", "prism", "--json"]:
                return completed(args, 1, stderr="Configuration file not found: /tmp/config.toml")
            raise AssertionError(f"unexpected mutation after failed inspection: {args}")

        with self.assertRaisesRegex(RuntimeError, "inspect"):
            install_mcp.ensure_registration(
                self.root, interpreter=self.python, codex_cli=self.codex, run=runner
            )
        self.assertEqual(len(self.calls), 1)

    def test_import_has_no_subprocess_or_configuration_side_effect(self):
        path = ROOT / "scripts/install_mcp.py"
        spec = importlib.util.spec_from_file_location("install_mcp_import_probe", path)
        module = importlib.util.module_from_spec(spec)
        with patch("subprocess.run", side_effect=AssertionError("import ran subprocess")), \
                patch("subprocess.check_call", side_effect=AssertionError("import changed config")):
            spec.loader.exec_module(module)


if __name__ == "__main__":
    unittest.main()
