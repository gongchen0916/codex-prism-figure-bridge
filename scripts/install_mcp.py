#!/usr/bin/env python3
"""Safely register this skill's local Prism MCP server with Codex."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


PYTHON_VERSION_CODE = "import platform; print(platform.python_version())"


def _executable(value, label: str) -> Path:
    path = Path(os.path.abspath(os.path.expanduser(os.fspath(value))))
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError(f"{label} is not an executable file: {path}")
    return path


def discover_codex(explicit=None) -> Path:
    if explicit is not None:
        return _executable(explicit, "Codex CLI")
    found = shutil.which("codex")
    if not found:
        raise RuntimeError("Codex CLI was not found on PATH; pass --codex explicitly")
    return _executable(found, "Codex CLI")


def _call(run, args):
    return run([os.fspath(item) for item in args], capture_output=True, text=True, timeout=30)


def _safe_registration(value: dict) -> dict:
    transport = value.get("transport") if isinstance(value, dict) else None
    if not isinstance(transport, dict):
        return {"type": None, "command": None, "args": []}
    args = transport.get("args")
    return {
        "type": transport.get("type") if isinstance(transport.get("type"), str) else None,
        "command": transport.get("command") if isinstance(transport.get("command"), str) else None,
        "args": list(args) if isinstance(args, list) and all(isinstance(item, str) for item in args) else [],
    }


def _get_registration(codex: Path, run) -> dict | None:
    process = _call(run, [codex, "mcp", "get", "prism", "--json"])
    if process.returncode:
        message = f"{process.stdout}\n{process.stderr}".casefold()
        missing_prism = any(re.search(pattern, message) for pattern in (
            r"\bmcp server\s+(?:named\s+)?['\"]?prism['\"]?\s+(?:was\s+)?not found\b",
            r"\bmcp server\s+(?:named\s+)?['\"]?prism['\"]?\s+does not exist\b",
            r"\bunknown mcp server\s+['\"]?prism['\"]?\b",
            r"\bno mcp server named\s+['\"]?prism['\"]?\b",
        ))
        if missing_prism:
            return None
        raise RuntimeError(f"Unable to inspect prism MCP registration (exit code {process.returncode})")
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Codex returned invalid JSON for prism MCP registration") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Codex returned an invalid prism MCP registration")
    return value


def _python_version(interpreter: Path, run) -> tuple[int, int, int]:
    process = _call(run, [interpreter, "-c", PYTHON_VERSION_CODE])
    if process.returncode:
        raise RuntimeError(f"Unable to run configured MCP interpreter (exit code {process.returncode})")
    try:
        parts = tuple(int(part) for part in process.stdout.strip().split(".")[:3])
    except ValueError as exc:
        raise RuntimeError("Configured MCP interpreter returned an invalid version") from exc
    if len(parts) < 2:
        raise RuntimeError("Configured MCP interpreter returned an invalid version")
    return (parts + (0, 0, 0))[:3]


def _same_path(left: str, right: Path) -> bool:
    try:
        return Path(left).expanduser().resolve(strict=False) == right.resolve(strict=False)
    except (OSError, RuntimeError):
        return False


def ensure_registration(skill_root, *, interpreter=None, codex_cli=None, run=subprocess.run) -> dict:
    """Preserve a matching registration, add only when absent, never remove."""
    root = Path(os.path.abspath(os.path.expanduser(os.fspath(skill_root))))
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError(f"Skill root is not a regular directory: {root}")
    server = root / "scripts/prism_mcp_server.py"
    if server.is_symlink() or not server.is_file():
        raise RuntimeError(f"Missing MCP server: {server}")
    codex = discover_codex(codex_cli)
    current_raw = _get_registration(codex, run)
    explicit_python = _executable(interpreter, "MCP interpreter") if interpreter is not None else None

    if current_raw is not None:
        current = _safe_registration(current_raw)
        server_matches = current["type"] == "stdio" and len(current["args"]) == 1 and _same_path(current["args"][0], server)
        command_matches = isinstance(current["command"], str) and (explicit_python is None or _same_path(current["command"], explicit_python))
        if server_matches and command_matches:
            configured_python = _executable(current["command"], "Configured MCP interpreter")
            version = _python_version(configured_python, run)
            if version < (3, 9, 0):
                raise RuntimeError("The MCP interpreter must be Python 3.9 or newer")
            return {"status": "unchanged", "changed": False, "current": current, "python_version": ".".join(map(str, version))}
        desired_python = explicit_python or Path(sys.executable).absolute()
        return {
            "status": "conflict",
            "changed": False,
            "current": current,
            "desired": {"type": "stdio", "command": os.fspath(desired_python), "args": [os.fspath(server)]},
            "message": "Existing prism registration differs and was preserved",
        }

    desired_python = explicit_python or Path(sys.executable).absolute()
    version = _python_version(desired_python, run)
    if version < (3, 9, 0):
        raise RuntimeError("The MCP interpreter must be Python 3.9 or newer")
    add = _call(run, [codex, "mcp", "add", "prism", "--", desired_python, server])
    if add.returncode:
        raise RuntimeError(f"Codex MCP add failed (exit code {add.returncode}); no remove was attempted")
    verified_raw = _get_registration(codex, run)
    if verified_raw is None:
        raise RuntimeError("Codex MCP add returned success but prism registration is absent")
    verified = _safe_registration(verified_raw)
    if not (verified["type"] == "stdio" and _same_path(verified.get("command") or "", desired_python) and len(verified["args"]) == 1 and _same_path(verified["args"][0], server)):
        raise RuntimeError("Codex MCP add returned success but verification did not match")
    return {"status": "added", "changed": True, "current": verified, "python_version": ".".join(map(str, version))}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--python", dest="interpreter")
    parser.add_argument("--codex", dest="codex_cli")
    args = parser.parse_args(argv)
    try:
        result = ensure_registration(args.skill_root, interpreter=args.interpreter, codex_cli=args.codex_cli)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"MCP registration failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if result["status"] == "conflict" else 0


if __name__ == "__main__":
    raise SystemExit(main())
