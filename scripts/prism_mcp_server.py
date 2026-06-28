#!/usr/bin/env python3
"""Local MCP server that lets Codex drive GraphPad Prism via Prism scripts.

GraphPad Prism does not expose a full external API. This server uses the stable
Prism automation route: patch data into a template-backed .pzfx, launch a .pzc
export script through Prism (no GUI click automation), and return SVG paths.
"""

from __future__ import annotations

import base64
import csv
import importlib.util
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
SKILL_ROOT = ROOT.parent
DEFAULT_OUTPUT_ROOT = SKILL_ROOT / "runs"
PYTHON = os.environ.get("PYTHON", sys.executable)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = load_module("prism_bridge", ROOT / "prism_bridge.py")
matcher = load_module("template_matcher", ROOT / "template_matcher.py")

TOOLS: list[dict[str, Any]] = [
    {
        "name": "prism_env",
        "description": "Check local GraphPad Prism bridge capabilities and Prism installation state.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "prism_list_templates",
        "description": "List automation-whitelisted GraphPad Prism template aliases.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "prism_match_template",
        "description": (
            "Recommend the best Prism template alias for CSV/TSV data shape and optional figure hints. "
            "Call this before prism_draw."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "data": {
                    "type": "string",
                    "description": "CSV/TSV table text, or a path to an existing CSV/TSV file.",
                },
                "data_is_path": {
                    "type": "boolean",
                    "description": "Set true when data is a filesystem path instead of inline CSV/TSV text.",
                    "default": False,
                },
                "hint": {
                    "type": "string",
                    "description": "Optional figure description such as 'box plot with significance stars'.",
                    "default": "",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of ranked matches to return.",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 10,
                },
                "automation_only": {
                    "type": "boolean",
                    "description": "Restrict matches to the automation whitelist.",
                    "default": True,
                },
            },
            "required": ["data"],
            "additionalProperties": False,
        },
    },
    {
        "name": "prism_draw",
        "description": (
            "Create a Prism project from CSV/TSV data, optionally run Prism to export SVG, "
            "and return the rendered SVG directly as MCP image content when available."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "data": {
                    "type": "string",
                    "description": "CSV/TSV table text, or a path to an existing CSV/TSV file.",
                },
                "data_is_path": {
                    "type": "boolean",
                    "description": "Set true when data is a filesystem path instead of inline CSV/TSV text.",
                    "default": False,
                },
                "template": {
                    "type": "string",
                    "description": "Template alias from prism_match_template or prism_list_templates.",
                    "default": "column-scatter",
                },
                "name": {
                    "type": "string",
                    "description": "Base filename for generated outputs.",
                    "default": "prism_figure",
                },
                "title": {
                    "type": "string",
                    "description": "Prism graph/table title.",
                    "default": "Prism Figure",
                },
                "outdir": {
                    "type": "string",
                    "description": "Output directory. Defaults to this server's runs directory.",
                },
                "run_prism": {
                    "type": "boolean",
                    "description": "Ask GraphPad Prism to export SVG using the generated .pzc script.",
                    "default": True,
                },
                "close_prism": {
                    "type": "boolean",
                    "description": "Close the Prism project after export. Leave false to keep Prism warm.",
                    "default": False,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Seconds to wait for Prism export script completion.",
                    "default": 120,
                    "minimum": 5,
                    "maximum": 600,
                },
                "return_image_data": {
                    "type": "boolean",
                    "description": "Include SVG bytes as MCP image content when exported.",
                    "default": True,
                },
                "make_pptx": {
                    "type": "boolean",
                    "description": "Also generate a one-slide PPTX containing the exported SVG and Prism source link.",
                    "default": False,
                },
            },
            "required": ["data"],
            "additionalProperties": False,
        },
    },
]


class McpError(Exception):
    def __init__(self, message: str, code: int = -32000) -> None:
        super().__init__(message)
        self.code = code


def log(message: str) -> None:
    print(f"[prism-mcp] {message}", file=sys.stderr, flush=True)


def send(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def text_content(text: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": text}]


def slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    value = value.strip("._-")
    return value or f"prism_{int(time.time())}"


def sniff_suffix(data: str) -> str:
    sample = data[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        return ".tsv" if dialect.delimiter == "\t" else ".csv"
    except csv.Error:
        return ".tsv" if "\t" in sample else ".csv"


def write_inline_data(data: str, name: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="prism_mcp_data_"))
    path = tmp / f"{slug(name)}{sniff_suffix(data)}"
    path.write_text(data.strip() + "\n", encoding="utf-8")
    return path


def resolve_data_path(args: dict[str, Any], name: str) -> Path:
    raw_data = str(args.get("data") or "")
    if not raw_data.strip():
        raise McpError("data must be non-empty")
    if bool(args.get("data_is_path", False)):
        data_path = Path(raw_data).expanduser().resolve()
        if not data_path.exists():
            raise McpError(f"Data path does not exist: {data_path}")
        return data_path
    return write_inline_data(raw_data, name)


def run_bridge_cli(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    cmd = [PYTHON, str(ROOT / "prism_bridge.py"), *args]
    log("running " + " ".join(cmd))
    return subprocess.run(
        cmd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )


def find_exports(outdir: Path, name: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for suffix, key in [
        (".pzfx", "project"),
        ("_export.pzc", "export_script"),
        (".svg", "svg"),
        (".pptx", "pptx"),
    ]:
        path = outdir / f"{name}{suffix}"
        if path.exists():
            found[key] = str(path)
    return found


def maybe_image_content(path: Path) -> dict[str, str] | None:
    if not path.exists() or not path.is_file():
        return None
    mime_type = mimetypes.guess_type(path.name)[0]
    if mime_type != "image/svg+xml":
        return None
    return {
        "type": "image",
        "mimeType": mime_type,
        "data": base64.b64encode(path.read_bytes()).decode("ascii"),
    }


def tool_prism_env(_: dict[str, Any]) -> list[dict[str, str]]:
    cp = run_bridge_cli(["env"], timeout=15)
    return text_content(cp.stdout.strip() or f"bridge exited with code {cp.returncode}")


def tool_prism_list_templates(_: dict[str, Any]) -> list[dict[str, str]]:
    cp = run_bridge_cli(["list-templates"], timeout=15)
    return text_content(cp.stdout.strip() or f"bridge exited with code {cp.returncode}")


def tool_prism_match_template(args: dict[str, Any]) -> list[dict[str, str]]:
    name = slug(str(args.get("name") or "match"))
    data_path = resolve_data_path(args, name)
    result = matcher.match_templates(
        SKILL_ROOT,
        data_path,
        hint=str(args.get("hint") or ""),
        limit=int(args.get("limit") or 5),
        automation_only=bool(args.get("automation_only", True)),
    )
    return text_content(json.dumps(result, indent=2, ensure_ascii=False))


def tool_prism_draw(args: dict[str, Any]) -> list[dict[str, str]]:
    name = slug(str(args.get("name") or "prism_figure"))
    title = str(args.get("title") or name)
    template = str(args.get("template") or "column-scatter")
    run_prism = bool(args.get("run_prism", True))
    close_prism = bool(args.get("close_prism", False))
    timeout = int(args.get("timeout") or 120)
    make_pptx = bool(args.get("make_pptx", False))
    return_image_data = bool(args.get("return_image_data", True))

    data_path = resolve_data_path(args, name)

    if args.get("outdir"):
        outdir = Path(str(args["outdir"])).expanduser().resolve()
    else:
        outdir = DEFAULT_OUTPUT_ROOT / f"{name}_{int(time.time())}"
    outdir.mkdir(parents=True, exist_ok=True)

    template_path = bridge.resolve_template(template)
    project = outdir / f"{name}.pzfx"
    patch_mode = bridge.build_project(template_path, data_path, project, title)
    script = bridge.create_export_script(project, outdir, name, keep_prism_warm=not close_prism)

    paths = {"project": str(project), "export_script": str(script), "patch_mode": patch_mode}
    prism_log = ""
    prism_ok = True

    if run_prism:
        prism_ok, prism_log = bridge.run_prism_script(script, timeout)
        paths.update(find_exports(outdir, name))

    svg_path = paths.get("svg")
    if make_pptx and svg_path:
        pptx_path = outdir / f"{name}.pptx"
        bridge.minimal_pptx(Path(svg_path), project, pptx_path, title)
        paths["pptx"] = str(pptx_path)

    paths.update(find_exports(outdir, name))
    svg_exists = "svg" in paths

    summary = {
        "ok": prism_ok if run_prism else True,
        "prism_run_ok": prism_ok if run_prism else None,
        "template": template,
        "title": title,
        "outdir": str(outdir),
        "export_format": "svg",
        "paths": paths,
        "prism_log": prism_log,
    }

    content: list[dict[str, str]] = [
        {"type": "text", "text": json.dumps(summary, indent=2, ensure_ascii=False)}
    ]
    if return_image_data and svg_exists:
        image = maybe_image_content(Path(paths["svg"]))
        if image:
            content.append(image)
    return content


def call_tool(name: str, args: dict[str, Any]) -> list[dict[str, str]]:
    if name == "prism_env":
        return tool_prism_env(args)
    if name == "prism_list_templates":
        return tool_prism_list_templates(args)
    if name == "prism_match_template":
        return tool_prism_match_template(args)
    if name == "prism_draw":
        return tool_prism_draw(args)
    raise McpError(f"Unknown tool: {name}", code=-32601)


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "prism-mcp-bridge", "version": "0.2.0"},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = request.get("params") or {}
        try:
            content = call_tool(str(params.get("name")), params.get("arguments") or {})
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": content, "isError": False},
            }
        except McpError as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": text_content(str(exc)), "isError": True},
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": text_content(str(exc)), "isError": True},
            }
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> int:
    log("server started")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = handle(request)
            if response is not None:
                send(response)
        except json.JSONDecodeError as exc:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}})
        except Exception as exc:
            log(f"unhandled error: {exc}")
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": str(exc)}})
    log("server stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
