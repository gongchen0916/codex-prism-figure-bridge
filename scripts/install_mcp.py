#!/usr/bin/env python3
"""Register this skill's local Prism MCP server with Codex."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    skill_root = Path(__file__).resolve().parents[1]
    server = skill_root / "scripts/prism_mcp_server.py"
    codex = shutil.which("codex") or "/Applications/Codex.app/Contents/Resources/codex"
    if not server.exists():
        print(f"Missing MCP server: {server}", file=sys.stderr)
        return 1
    subprocess.run([codex, "mcp", "remove", "prism"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.check_call([codex, "mcp", "add", "prism", "--", sys.executable, str(server)])
    subprocess.check_call([codex, "mcp", "get", "prism"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
