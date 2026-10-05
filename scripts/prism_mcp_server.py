#!/usr/bin/env python3
"""Local MCP server that lets Codex drive GraphPad Prism via Prism scripts.

GraphPad Prism does not expose a full external API. This server uses the stable
Prism automation route: patch data into a template-backed .pzfx, launch a .pzc
export script through Prism (no GUI click automation), and return SVG paths.
"""

from __future__ import annotations

import base64
import hashlib
import csv
import importlib.util
import json
import mimetypes
import os
import re
import shutil
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
SERVER_VERSION = "0.9.1"
# Compile the bootstrap helper from the exact bytes used for its identity.
# A normal import can reuse foreign modules or timestamp-valid stale bytecode.
_runtime_path = ROOT / 'runtime_state.py'
_runtime_source = _runtime_path.read_bytes()
_runtime_spec = importlib.util.spec_from_file_location('runtime_state', _runtime_path)
_runtime_module = importlib.util.module_from_spec(_runtime_spec)
exec(compile(_runtime_source, str(_runtime_path), 'exec', dont_inherit=True), _runtime_module.__dict__)
RuntimeState = _runtime_module.RuntimeState
RuntimeRestartRequired = _runtime_module.RuntimeRestartRequired
RUNTIME_STATE = RuntimeState(SKILL_ROOT, version=SERVER_VERSION)
RUNTIME_STATE.bind_server(globals(), sys._getframe().f_code, _runtime_module, _runtime_source)


def load_module(name: str, path: Path):
    if path.resolve() != ROOT / (name + '.py'):
        raise ValueError('Runtime module must belong to this installation')
    return RUNTIME_STATE.load_module(name)


bridge = load_module("prism_bridge", ROOT / "prism_bridge.py")
matcher = load_module("template_matcher", ROOT / "template_matcher.py")
error_receipts = load_module('mcp_error_receipts', ROOT / 'mcp_error_receipts.py')
RUNTIME_STATE.finish_loading({'bridge': bridge, 'matcher': matcher, 'error_receipts': error_receipts})

TOOLS: list[dict[str, Any]] = [
    {
        'name':'prism_windows_catalog',
        'description':'Search source-contract templates with separately labelled historical Windows native/PPT evidence. Compact results, exact IDs, no guessed fallback or arbitrary recolor claim.',
        'inputSchema':{'type':'object','properties':{
            'query':{'type':'string','default':''},'template_id':{'type':'string'},
            'groups':{'type':'integer','minimum':1},'data_profile':{'type':'string'},
            'limit':{'type':'integer','minimum':1,'maximum':20,'default':5},
            'offset':{'type':'integer','minimum':0,'default':0},
            'windows_recorded_only':{'type':'boolean','default':True}},'additionalProperties':False},
    },
    {
        'name':'prism_windows_scope',
        'description':'Audit actual library source hashes, declared conversion lineage and historical receipts. Separate index paths, source entries and alias counts; no application launch or new admission.',
        'inputSchema':{'type':'object','properties':{
            'view':{'type':'string','enum':['summary','sources','diagnostics','receipt_failures'],'default':'summary'},
            'source':{'type':'string'},'limit':{'type':'integer','minimum':1,'maximum':100,'default':20},
            'offset':{'type':'integer','minimum':0,'default':0},'refresh':{'type':'boolean','default':False}},'additionalProperties':False},
    },
    {
        'name':'prism_windows_blueprint',
        'description':'Read the compact exact source input blueprint: table dimensions, required graph/statistical/source-option fields, without scientific example data. Not preflight, native execution or approval.',
        'inputSchema':{'type':'object','properties':{'template_id':{'type':'string'}},'required':['template_id'],'additionalProperties':False},
    },
    {
        'name':'prism_windows_prepare',
        'description':'Prepare and fully preflight a new source-bound data replacement request from an exact template ID. Requires complete source table roles; retains source graph/layout/analysis policy, never runs Prism.',
        'inputSchema':{'type':'object','properties':{
            'template_id':{'type':'string'},'replacements':{'type':'object'},'graph_settings':{'type':'object'},
            'statistics':{'type':'object'},'source_options':{'type':'object','description':'Fresh complete source-specific panel labels/images/options when required by the blueprint; not source/policy overrides.'},'output_request':{'type':'string','description':'New absolute JSON output path; existing data/results are not overwritten.'}},
            'required':['template_id','replacements','graph_settings','output_request'],'additionalProperties':False},
    },
    {
        'name':'prism_windows_run',
        'description':'Run an explicit source-bound request with full new Windows native data/geometry/statistics/save-reopen verification, then create actual Prism OLE PowerPoint at native SVG physical size. No screenshot substitute.',
        'inputSchema':{'type':'object','properties':{
            'request':{'type':'string','description':'Absolute preflighted source request JSON path.'},
            'output':{'type':'string','description':'New absolute output directory.'},
            'make_pptx':{'type':'boolean','default':True}},'required':['request','output'],'additionalProperties':False},
    },
    {
        'name':'prism_recover_execution',
        'description':'Reconcile an exact uncertain Prism operation. Never kills Prism or retries a script. Recovery requires verified late completion or observed Prism process exit; previous outputs still need verification.',
        'inputSchema':{'type':'object','properties':{'operation_id':{'type':'string','description':'Exact operation_id reported by prism_env.execution.'}},'required':['operation_id'],'additionalProperties':False},
    },
    {
        "name": "prism_env",
        "description": "Check local GraphPad Prism bridge capabilities and Prism installation state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "runtime_only": {"type": "boolean", "default": False,
                                 "description": "Check loaded code identity without querying or launching Prism."}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "prism_list_templates",
        "description": "List automation-whitelisted GraphPad Prism template aliases.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "prism_list_template_catalog",
        "description": (
            "List indexed Prism templates with explicit safe-automation, experimental-preview, "
            "or reference-only status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "default": 100,
                    "minimum": 1,
                    "maximum": 200,
                },
                "preview_patchable_only": {"type": "boolean", "default": False},
                "offset": {"type": "integer", "default": 0, "minimum": 0},
                "query": {
                    "type": "string",
                    "description": "Case-insensitive alias or original template-name search.",
                },
            },
            "additionalProperties": False,
        },
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
                "first_row_is_data": {
                    "type": "boolean",
                    "description": (
                        "Required only when the first row is entirely numeric. Set false for "
                        "numeric column headers (years/doses), or true for headerless observations."
                    ),
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
            "and return a readable PNG preview together with the native SVG path."
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
                "first_row_is_data": {
                    "type": "boolean",
                    "description": (
                        "Required only when the first row is entirely numeric. Set false for "
                        "numeric column headers (years/doses), or true for headerless observations."
                    ),
                },
                "template": {
                    "type": "string",
                    "description": "Template alias from prism_match_template or prism_list_templates.",
                    "default": "column-points",
                },
                "hint": {
                    "type": "string",
                    "description": "Use the same figure hint passed to prism_match_template so data profiling stays consistent.",
                    "minLength": 1,
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
                "x_axis_title": {
                    "type": "string",
                    "description": "Optional X-axis title override; inferred from an XY X header when omitted.",
                },
                "y_axis_title": {
                    "type": "string",
                    "description": "Optional Y-axis title override; inferred from the Y header for XY data.",
                },
                "auto_axis": {
                    "type": "boolean",
                    "description": "Override fixed template Y limits from the supplied data.",
                    "default": True,
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
                    "description": "Close the Prism project after export. Defaults true to avoid Prism queue buildup.",
                    "default": True,
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
                    "description": "Include readable PNG image content rendered from the exported SVG.",
                    "default": True,
                },
                "make_pptx": {
                    "type": "boolean",
                    "description": "Generate a one-slide PPTX in Windows PowerPoint with a real editable Prism OLE object; requires successful native export.",
                    "default": False,
                },
            },
            "required": ["data", "hint"],
            "additionalProperties": False,
        },
    },
    {
        "name": "prism_preview_template",
        "description": (
            "Experimentally patch and render a structurally compatible indexed Prism template. "
            "Outputs are unverified and always require manual review; this never bypasses prism_draw's safe whitelist."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "data": {
                    "type": "string",
                    "description": "CSV/TSV table text, or a path to an existing CSV/TSV file.",
                },
                "data_is_path": {"type": "boolean", "default": False},
                "data_format": {
                    "type": "string",
                    "enum": ["wide_numeric", "long_twoway"],
                    "default": "wide_numeric",
                },
                "first_row_is_data": {"type": "boolean"},
                "template": {
                    "type": "string",
                    "description": "Indexed template alias selected with prism_match_template automation_only=false.",
                },
                "hint": {"type": "string", "minLength": 1},
                "acknowledge_unverified": {
                    "type": "boolean",
                    "const": True,
                    "description": "Must be true to acknowledge that this is not publication-safe automation.",
                },
                "name": {"type": "string", "default": "prism_preview"},
                "title": {"type": "string", "default": "Prism Template Preview"},
                "x_axis_title": {"type": "string"},
                "y_axis_title": {"type": "string"},
                "auto_axis": {"type": "boolean", "default": True},
                "outdir": {"type": "string"},
                "run_prism": {"type": "boolean", "default": True},
                "close_prism": {"type": "boolean", "default": True},
                "timeout": {
                    "type": "integer",
                    "default": 120,
                    "minimum": 5,
                    "maximum": 600,
                },
            },
            "required": ["data", "template", "hint", "acknowledge_unverified"],
            "additionalProperties": False,
        },
    },
]


STYLE_PROPERTIES = {
    "palette": {
        "type": "string",
        "description": "Registered Prism scheme name, or inherit (default).",
    },
    "group_colors": {
        "type": "object",
        "description": "Exact group-name to #RRGGBB map; axes and text remain black.",
        "additionalProperties": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"},
    },
    "graph_index": {"type": "integer", "minimum": 1, "maximum": 100, "default": 1},
    "title_mode": {
        "type": "string",
        "enum": ["auto", "native", "overlay"],
        "default": "auto",
        "description": "native requires titles visible inside the Prism project. auto adds only missing SVG titles and reports the difference.",
    },
}
for _tool in TOOLS:
    if _tool["name"] in {"prism_draw", "prism_preview_template"}:
        _tool["inputSchema"]["properties"].update(STYLE_PROPERTIES)
TOOLS.append(
    {
        "name": "prism_list_palettes",
        "description": "List registered native Prism schemes and exact group colors.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }
)
TOOLS.extend(
    [
        {
            "name": "prism_template_thumbnail",
            "description": "Show a cached native preview of a template before choosing it; labels/statistics are reference content only.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "template": {"type": "string"},
                    "graph_index": {"type": "integer", "minimum": 1, "maximum": 100},
                    "refresh": {"type": "boolean"},
                    "timeout": {"type": "integer", "minimum": 5, "maximum": 120},
                },
                "required": ["template"],
                "additionalProperties": False,
            },
        },
        {
            "name": "prism_redraw",
            "description": "Reuse a saved figure specification with optional new data and colors; creates a new output folder unless specified.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "spec": {"type": "string"},
                    "data": {"type": "string"},
                    "data_is_path": {"type": "boolean"},
                    "outdir": {"type": "string"},
                    "run_prism": {"type": "boolean"},
                    "title": {"type": "string"},
                    "name": {"type": "string"},
                    "group_colors": STYLE_PROPERTIES["group_colors"],
                    "palette": STYLE_PROPERTIES["palette"],
                },
                "required": ["spec"],
                "additionalProperties": False,
            },
        },
        {
            "name": "prism_export_project",
            "description": "Re-export an existing edited Prism project without replacing its data or template style.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "outdir": {"type": "string"},
                    "name": {"type": "string"},
                    "graph_index": STYLE_PROPERTIES["graph_index"],
                    "timeout": {"type": "integer", "minimum": 5, "maximum": 600},
                },
                "required": ["project"],
                "additionalProperties": False,
            },
        },
    ]
)
TOOLS.append(
    {
        "name": "prism_inspect_template",
        "description": "Inspect native graph pages, hidden analyses and Info/layout pages without opening Prism.",
        "inputSchema": {
            "type": "object",
            "properties": {"template": {"type": "string"}},
            "required": ["template"],
            "additionalProperties": False,
        },
    }
)


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
    value = value[:80]
    return value or f"prism_{int(time.time())}"


def validate_tool_arguments(tool_name: str, args: Any) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise McpError(f"Arguments for {tool_name} must be a JSON object")
    tool = next((item for item in TOOLS if item["name"] == tool_name), None)
    if tool is None:
        raise McpError(f"Unknown tool: {tool_name}", code=-32601)

    schema = tool["inputSchema"]
    properties = schema.get("properties", {})
    unknown = sorted(set(args) - set(properties))
    if unknown and schema.get("additionalProperties") is False:
        raise McpError(f"Unknown argument(s) for {tool_name}: {', '.join(unknown)}")
    missing = [name for name in schema.get("required", []) if name not in args]
    if missing:
        raise McpError(
            f"Missing required argument(s) for {tool_name}: {', '.join(missing)}"
        )

    for name, value in args.items():
        rule = properties.get(name, {})
        expected = rule.get("type")
        valid = (
            (expected == "string" and isinstance(value, str))
            or (expected == "boolean" and type(value) is bool)
            or (expected == "integer" and type(value) is int)
            or (expected == "object" and isinstance(value, dict))
            or expected is None
        )
        if not valid:
            raise McpError(f"Argument {name!r} for {tool_name} must be {expected}")
        if "const" in rule and value != rule["const"]:
            raise McpError(
                f"Argument {name!r} for {tool_name} must be {rule['const']!r}"
            )
        if "enum" in rule and value not in rule["enum"]:
            raise McpError(
                f"Argument {name!r} for {tool_name} must be one of {rule['enum']!r}"
            )
        if (
            expected == "string"
            and "minLength" in rule
            and len(value.strip()) < rule["minLength"]
        ):
            raise McpError(
                f"Argument {name!r} for {tool_name} must contain at least "
                f"{rule['minLength']} non-whitespace character"
            )
        if expected == "integer":
            if "minimum" in rule and value < rule["minimum"]:
                raise McpError(
                    f"Argument {name!r} for {tool_name} must be at least {rule['minimum']}"
                )
            if "maximum" in rule and value > rule["maximum"]:
                raise McpError(
                    f"Argument {name!r} for {tool_name} must be at most {rule['maximum']}"
                )
    return args


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
        if not data_path.is_file():
            raise McpError(f"Data path is not an existing file: {data_path}")
        return data_path
    return write_inline_data(raw_data, name)


def cleanup_inline_data(args: dict[str, Any], data_path: Path) -> None:
    if not bool(args.get("data_is_path", False)):
        shutil.rmtree(data_path.parent, ignore_errors=True)


def cleanup_stale_inline_data(max_age_seconds: int = 24 * 60 * 60) -> int:
    temp_root = Path(tempfile.gettempdir())
    now = time.time()
    removed = 0
    for path in temp_root.glob("prism_mcp_data_*"):
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            expired = now - path.stat().st_mtime >= max_age_seconds
        except OSError:
            continue
        if not expired:
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            removed += 1
    return removed


def run_bridge_cli(
    args: list[str], timeout: int | None = None
) -> subprocess.CompletedProcess[str]:
    cmd = [PYTHON, str(ROOT / "runtime_state.py"), '--root', str(SKILL_ROOT),
           '--expected-identity', RUNTIME_STATE.status()['loaded_identity'],
           '--execute', 'prism_bridge', *args]
    log("running " + " ".join(cmd))
    return subprocess.run(
        cmd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        close_fds=True,
        stdin=subprocess.DEVNULL,
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
    if mime_type not in {"image/svg+xml", "image/png"}:
        return None
    return {
        "type": "image",
        "mimeType": mime_type,
        "data": base64.b64encode(path.read_bytes()).decode("ascii"),
    }


def tool_prism_env(args: dict[str, Any]) -> list[dict[str, str]]:
    bridge_health=load_module('bridge_health',ROOT/'bridge_health.py')
    runtime = RUNTIME_STATE.status()
    target=bridge.execution_target()
    native=bridge.execution_status(passive=True)
    ppt=bridge.windows_prism_ppt.execution_status()
    probe=bool(target['ready']and not args.get('runtime_only')and not runtime['restart_required'])
    health=bridge_health.status(runtime,target,native,ppt,probe=probe)
    if not target['ready']:
        return text_content(json.dumps({'runtime':runtime,'execution_target':target,'prism_checked':False,'health':health,'ppt_execution':ppt,
            'execution':native,'native_execution_ready':False},ensure_ascii=False))
    if args.get('runtime_only') or runtime['restart_required']:
        return text_content(json.dumps({'runtime': runtime, 'prism_checked': False,'health':health,'ppt_execution':ppt,
            'execution':native}, ensure_ascii=False))
    result={'platform':bridge.platform.system(),'execution_target':target,'native_execution_ready':target['ready'],
            'export_format':'svg','ppt_editable_ole':'Windows native OLE','health':health,'ppt_execution':ppt}
    result.update(runtime=runtime, prism_checked=True)
    result.update(execution=native,prism_processes={'available':health['probe']['guest_reachable'],'instances':health['probe']['instances']})
    result['prism_application']={'backend':'windows_vm','executable':target.get('prism_exe'),'sha256':target.get('prism_sha256')}
    return text_content(json.dumps(result, ensure_ascii=False))


def tool_prism_list_templates(_: dict[str, Any]) -> list[dict[str, str]]:
    cp = run_bridge_cli(["list-templates"], timeout=15)
    import published_templates
    extra = [f"{e['alias']:32} published_inherited  {e['name']}  {json.dumps(published_metadata(e), ensure_ascii=False)}"
             for e in published_templates.list_published(SKILL_ROOT)]
    return text_content('\n'.join([cp.stdout.strip() or f"bridge exited with code {cp.returncode}"] + extra))


def published_entry(identifier):
    if not identifier:
        return None
    import published_templates
    return published_templates.resolve_published(SKILL_ROOT, str(identifier))


def published_metadata(entry):
    caps = entry['capabilities']
    metadata = {
        'alias': entry['alias'], 'name': entry['name'], 'source_name': entry['name'],
        'collection': 'published', 'kind': 'xml',
        'plot_type': entry['kind'],
        'table_type': 'XY' if entry['kind'] == 'stacked_xy' else 'TwoWay',
        'has_x_column': entry['kind'] == 'stacked_xy', 'y_column_count': caps['series_count'],
        'graph_count': 1, 'analysis_count': 0, 'native_titles': bool(caps.get('graph_title')),
        'automation_status': 'published_inherited', 'exact_hex_colors': False,
        'capabilities': caps, 'template_sha256': entry['seed_sha256'],
        'preview_patchable': False,
        'preview_path': str((SKILL_ROOT / entry['seed']).with_suffix('.png')),
    }
    if entry['kind'] == 'mean_heatmap':
        metadata.update(aggregation=caps['aggregation'], color_scale_mode=caps['color_scale_mode'])
    return metadata


def draw_published_entry(args, entry):
    import published_templates
    name = slug(str(args.get('name') or 'prism_figure'))
    data_path = resolve_data_path(args, name)
    try:
        options = {k: v for k, v in args.items() if k not in {'template', 'data', 'data_is_path', 'name', 'outdir'}}
        wants_ppt = bool(options.pop('make_pptx', False))
        if wants_ppt and not bool(options.get('run_prism', True)):
            raise ValueError('Inherited PPT output requires a verified native export; no fallback plot is substituted')
        # Validate before allocating output or invoking any native process.
        published_templates.validate_request(SKILL_ROOT, entry, data_path, options)
        if args.get('outdir'):
            outdir = Path(str(args['outdir'])).expanduser().resolve()
        else:
            DEFAULT_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
            outdir = Path(tempfile.mkdtemp(prefix=name + '_', dir=DEFAULT_OUTPUT_ROOT)) / 'figure'
        result = published_templates.draw_published(SKILL_ROOT, entry, data_path, outdir, name, options,
                                                   exporter=bridge.run_prism_export_staged)
        if wants_ppt:
            if not result.get('ok') or not result.get('source_matches_export') or not result['paths'].get('svg'):
                raise ValueError('Cannot package an unverified inherited graph in PPT')
            pptx = outdir / (name + '.pptx')
            bridge.windows_prism_ppt.package(Path(result['paths']['project']),pptx,str(args.get('title') or entry['name']))
            result['paths']['pptx'] = str(pptx)
            result['pptx_image_source'] = 'windows_prism_ole'
        saved_options = {k: v for k, v in args.items() if k not in {'outdir', 'data', 'data_is_path'}}
        saved_options.update(template=entry['alias'], data=data_path.read_text(encoding='utf-8-sig'), data_is_path=False)
        spec = Path(result['paths']['spec'])
        specification = json.loads(spec.read_text(encoding='utf-8'))
        specification.update(version=1, tool='prism_draw', template_sha256=entry['seed_sha256'])
        specification['arguments'] = {**specification.get('arguments', {}), **saved_options}
        if 'graph_title' in specification['arguments']:
            specification['arguments']['title'] = specification['arguments'].pop('graph_title')
        spec.write_text(json.dumps(specification, ensure_ascii=False, indent=2), encoding='utf-8')
        content = text_content(json.dumps(result, ensure_ascii=False, indent=2))
        if args.get('return_image_data', True) and result.get('ok') and result['paths'].get('preview_png'):
            img = maybe_image_content(Path(result['paths']['preview_png']))
            if img:
                content.append(img)
        return content
    finally:
        cleanup_inline_data(args, data_path)


def tool_prism_list_template_catalog(args: dict[str, Any]) -> list[dict[str, str]]:
    import template_gallery
    from template_identity import resolve_identity, requested_template_number
    import published_templates

    limit = int(args.get("limit") or 100)
    offset = int(args.get("offset", 0))
    preview_only = bool(args.get("preview_patchable_only", False))
    query = str(args.get("query") or "").strip().lower()
    index = matcher.load_index(SKILL_ROOT)
    curated = matcher.load_curated(SKILL_ROOT)
    inherited_identities = published_templates.list_published_identities(SKILL_ROOT)
    identity = resolve_identity(index.get('templates', []) + inherited_identities, query)
    identity_filter = identity['kind'] != 'none' or requested_template_number(query) is not None
    identity_aliases = {entry.get('alias') for entry in identity['candidates']}

    def selected_identity(entry):
        return (entry.get('alias') in identity_aliases if identity_filter
                else matcher.matches_query(entry, query))
    inherited = published_templates.list_published(SKILL_ROOT, identifiers=[
        entry['alias'] for entry in inherited_identities if not preview_only and selected_identity(entry)])
    inherited_metadata = [published_metadata(entry) for entry in inherited]
    safe = set(curated.get("whitelist_automation", []))
    rows: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    preview_count = 0
    safe_count = 0
    for entry in index.get("templates", []):
        preview_patchable = (
            entry.get("kind") == "xml"
            and entry.get("table_count") == 1
            and entry.get("table_type") in {"OneWay", "XY", "TwoWay"}
            and int(entry.get("y_column_count") or 0) > 0
        )
        if preview_patchable:
            preview_count += 1
        if entry.get("alias") in safe:
            safe_count += 1
        if preview_only and not preview_patchable:
            continue
        if not selected_identity(entry):
            continue
        status = (
            "safe_automation"
            if entry.get("alias") in safe
            else "experimental_preview" if preview_patchable else "reference_only"
        )
        rows.append(
            ({
                "alias": entry.get("alias"),
                "name": entry.get("name"),
                "source_alias": entry.get("source_alias"),
                "source_name": entry.get("source_name"),
                "collection": entry.get("collection"),
                "kind": entry.get("kind"),
                "table_type": entry.get("table_type"),
                "has_x_column": entry.get("has_x_column"),
                "y_column_count": entry.get("y_column_count"),
                "preview_patchable": preview_patchable,
                "automation_status": status,
                "preview_path": None,
                "graph_count": entry.get("graph_count"),
                "analysis_count": entry.get("analysis_count"),
                "native_titles": entry.get("native_titles", False),
                "exact_hex_colors": bool(entry.get("color_binding")),
            }, entry)
        )
    for metadata in inherited_metadata:
        if not preview_only and selected_identity(metadata):
            rows.append((metadata, None))
    rows.sort(
        key=lambda item: (
            item[0]["automation_status"] not in {"safe_automation", "published_inherited"},
            item[0]["alias"] or "",
        )
    )
    selected = []
    for metadata, entry in rows[offset : offset + limit]:
        if entry is not None:
            preview = template_gallery.preview_path(entry)
            metadata["preview_path"] = str(preview) if preview.exists() else None
        selected.append(metadata)
    result = {
        "counts": {
            "indexed": len(index.get("templates", [])) + len(inherited_identities),
            "safe_automation": safe_count + len(inherited_identities),
            "published_inherited": len(inherited_identities),
            "preview_patchable": preview_count,
            "reference_only": len(index.get("templates", [])) - preview_count,
        },
        "preview_patchable_only": preview_only,
        "identity_kind": identity['kind'],
        "requested_identity": query if identity_filter else None,
        "published_validation": {"scope": "matched_entries_only", "validated": len(inherited),
                                 "declared": len(inherited_identities)},
        "templates": selected,
        "matched_count": len(rows),
        "next_offset": offset + limit if offset + limit < len(rows) else None,
        "truncated": offset + limit < len(rows),
    }
    return text_content(json.dumps(result, indent=2, ensure_ascii=False))


def tool_prism_match_template(args: dict[str, Any]) -> list[dict[str, str]]:
    name = slug(str(args.get("name") or "match"))
    data_path = resolve_data_path(args, name)
    first_row_is_data = (
        args.get("first_row_is_data") if "first_row_is_data" in args else None
    )
    try:
        published = published_entry(str(args.get('hint') or '').strip())
        if published:
            import published_templates
            published_templates.validate_request(SKILL_ROOT, published, data_path,
                                                  {'first_row_is_data': first_row_is_data, 'hint': str(args.get('hint') or published['name'])})
            result = {'recommended': published['alias'], 'matches': [published_metadata(published)],
                      'ambiguous': False, 'needs_hint': False, 'selection_basis': 'published_explicit_name',
                      'automation_only': bool(args.get('automation_only', True))}
            return text_content(json.dumps(result, ensure_ascii=False, indent=2))
        result = matcher.match_templates(
            SKILL_ROOT,
            data_path,
            hint=str(args.get("hint") or ""),
            limit=int(args.get("limit") or 5),
            automation_only=bool(args.get("automation_only", True)),
            first_row_is_data=first_row_is_data,
        )
    finally:
        cleanup_inline_data(args, data_path)
    return text_content(json.dumps(result, indent=2, ensure_ascii=False))


def drawing_style(args, data_path):
    if args.get("data_format") == "long_twoway":
        _, rows = bridge.split_header(bridge.read_table(data_path), False)
        headers = list(dict.fromkeys(row[1] for row in rows))
    else:
        rows = bridge.read_table(data_path)
        profile = matcher.infer_data_profile(
            rows, str(args.get("hint") or ""), args.get("first_row_is_data")
        )
        headers = (
            profile["headers"][1:] if profile["has_x_column"] else profile["headers"]
        )
    if args.get("group_colors") is not None:
        palette = {
            "name": None,
            "path": None,
            "group_colors": bridge.template_styles.validate_colors(
                headers, args["group_colors"]
            ),
            "group_order": headers,
        }
    else:
        palette = bridge.native_palette.prepare_palette(headers, args.get("palette"))
    return (
        palette,
        {"palette_name": palette["name"], "graph_index": args.get("graph_index", 1)},
        args.get("title_mode", "auto"),
    )


def tool_prism_draw(args: dict[str, Any]) -> list[dict[str, str]]:
    RUNTIME_STATE.assert_current()
    entry = published_entry(args.get('template') or str(args.get('hint') or '').strip())
    if entry:
        return draw_published_entry(args, entry)
    name = slug(str(args.get("name") or "prism_figure"))
    title = str(args.get("title") or name)
    template = str(args.get("template") or "")
    hint = str(args.get("hint") or "")
    run_prism = bool(args.get("run_prism", True))
    close_prism = bool(args.get("close_prism", True))
    timeout = int(args.get("timeout") or 120)
    make_pptx = bool(args.get("make_pptx", False))
    return_image_data = bool(args.get("return_image_data", True))
    auto_axis = bool(args.get("auto_axis", True))
    first_row_is_data = (
        args.get("first_row_is_data") if "first_row_is_data" in args else None
    )

    data_path = resolve_data_path(args, name)
    outdir: Path | None = None
    auto_created_outdir = False
    try:
        if not template:
            match = matcher.match_templates(
                SKILL_ROOT, data_path, hint, first_row_is_data=first_row_is_data
            )
            template = match.get("recommended")
            if not template:
                raise ValueError(
                    "Blocked figure type: "
                    + (match.get("blocked_reason") or "No compatible template")
                )
        else:
            from template_identity import resolve_identity, requested_template_number
            identity = resolve_identity(matcher.load_index(SKILL_ROOT).get('templates', []), template)
            if identity['kind'] == 'ambiguous':
                raise ValueError('Template identity is ambiguous; choose an exact alias: ' +
                                 ', '.join(entry['alias'] for entry in identity['candidates']))
            if identity['candidates']:
                template = identity['candidates'][0]['alias']
            elif requested_template_number(template) is not None:
                raise ValueError('Unknown template number: ' + template)
        template_path = bridge.resolve_template(template)
        entry = bridge.automation_template_entries().get(template_path)
        if entry is None:
            raise ValueError('Template is reference-only or not automation-whitelisted: ' + template)
        profile = matcher.infer_data_profile(bridge.read_table(data_path), hint, first_row_is_data)
        compatible = matcher.compatibility_result(
            entry, profile, matcher.load_curated(SKILL_ROOT).get('automation_limits', {}))
        if not compatible['compatible']:
            raise ValueError('Template incompatible: ' + ' '.join(compatible['reasons']))
        palette, style, title_mode = drawing_style(args, data_path)
        if args.get("outdir"):
            outdir = Path(str(args["outdir"])).expanduser().resolve()
        else:
            DEFAULT_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
            outdir = Path(tempfile.mkdtemp(prefix=f"{name}_", dir=DEFAULT_OUTPUT_ROOT))
            auto_created_outdir = True

        entry = bridge.indexed_template_entries().get(template_path, {})
        if entry.get("graph_count") and style["graph_index"] > entry["graph_count"]:
            raise ValueError(
                f"Template contains {entry['graph_count']} graph(s); graph_index is out of range"
            )
        if entry.get("native_titles") and "title_mode" not in args:
            title_mode = "native"
        project = outdir / f"{name}.pzfx"
        patch_mode = bridge.build_project(
            template_path,
            data_path,
            project,
            title,
            hint=hint,
            first_row_is_data=first_row_is_data,
        )
        if palette.get("group_colors") is not None:
            entry = bridge.indexed_template_entries()[template_path]
            palette = bridge.template_styles.apply_colors(
                project, entry["alias"], palette["group_order"], palette["group_colors"]
            )
        default_x_title, default_y_title = bridge.default_axis_titles(
            data_path, first_row_is_data, hint
        )
        x_axis_title = (
            str(args["x_axis_title"]) if "x_axis_title" in args else default_x_title
        )
        y_axis_title = (
            str(args["y_axis_title"]) if "y_axis_title" in args else default_y_title
        )
        y_axis_limits = (
            bridge.numeric_axis_limits(
                bridge.read_table(data_path),
                first_row_is_data,
                has_x_column=default_x_title is not None,
            )
            if auto_axis
            else None
        )
        if y_axis_limits and (entry.get("alias") or template) == "watercolor-bars-sem":
            lo, hi, step = y_axis_limits
            y_axis_limits = (min(0, lo), max(0, hi), step)
        x_axis_limits = (
            bridge.numeric_x_limits(bridge.read_table(data_path), first_row_is_data)
            if auto_axis and (entry.get("alias") or template) == "watercolor-xy-lines"
            else None
        )
        script = bridge.create_export_script(
            project,
            outdir,
            name,
            keep_prism_warm=not close_prism,
            graph_title=title,
            x_axis_title=x_axis_title,
            y_axis_title=y_axis_title,
            y_axis_limits=y_axis_limits,
            x_axis_limits=x_axis_limits,
            **style,
        )

        paths = {
            "project": str(project),
            "export_script": str(script),
            "patch_mode": patch_mode,
        }
        prism_log = ""
        prism_ok = True
        fresh_svg: Path | None = None

        if run_prism:
            prism_ok, prism_log, exported_svg = bridge.run_prism_export_staged(
                project,
                outdir,
                name,
                timeout=timeout,
                keep_prism_warm=not close_prism,
                graph_title=title,
                x_axis_title=x_axis_title,
                y_axis_title=y_axis_title,
                y_axis_limits=y_axis_limits,
                x_axis_limits=x_axis_limits,
                title_mode=title_mode,
                **style,
            )
            if prism_ok and exported_svg.exists():
                if palette.get("group_colors"):
                    requested = set(palette["group_colors"].values())
                    painted = set(bridge.svg_render.inspect(exported_svg)["colors"])
                    if not requested.issubset(painted) or not painted.issubset(
                        requested | {"#000000", "#FFFFFF"}
                    ):
                        raise ValueError(
                            "Prism rendered missing or unexpected group colors; refusing to report success"
                        )
                fresh_svg = exported_svg
                paths["svg"] = str(exported_svg)
                if return_image_data:
                    png = exported_svg.with_suffix(".png")
                    bridge.svg_render.render(exported_svg, png)
                    paths["preview_png"] = str(png)

        pptx_image_source = None
        if make_pptx:
            pptx_image = fresh_svg
            if pptx_image is None:
                raise ValueError('Windows editable PPT requires a fresh successful native Prism export; no fallback is embedded')
            else:
                pptx_image_source = "windows_prism_ole"
            pptx_path = outdir / f"{name}.pptx"
            bridge.windows_prism_ppt.package(project,pptx_path,title)
            paths["pptx"] = str(pptx_path)
            paths["pptx_image"] = str(pptx_image)

        svg_exists = "svg" in paths
        spec_args = {
            key: value
            for key, value in args.items()
            if key not in {"outdir", "data", "data_is_path"}
        }
        spec_args.update(
            data=data_path.read_text(encoding="utf-8-sig"),
            data_is_path=False,
            template=entry.get("alias") or template,
            hint=hint,
        )
        spec_path = outdir / f"{name}.spec.json"
        spec_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "tool": "prism_draw",
                    "template_sha256": hashlib.sha256(
                        template_path.read_bytes()
                    ).hexdigest(),
                    "arguments": spec_args,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        paths["spec"] = str(spec_path)

        summary = {
            "plot_definition": {
                "watercolor-box": "Tukey box and whiskers",
                "watercolor-violin": "violin with all points",
                "watercolor-bars-sem": "mean ± SEM with all points",
                "watercolor-paired": "paired rows, symbols and lines",
                "watercolor-points": "individual values",
                "watercolor-xy-lines": "linear XY points and connecting lines; gaps for missing Y; no curve fitting",
            }.get(entry.get("alias") or template),
            "native_style": palette,
            "graph_index": style["graph_index"],
            "source_matches_export": (
                "Native SVG titles verified." in prism_log if run_prism else None
            ),
            "ok": (prism_ok if run_prism else True)
            and pptx_image_source != "fallback_svg",
            "prism_run_ok": prism_ok if run_prism else None,
            "template": template,
            "hint": hint,
            "first_row_is_data": first_row_is_data,
            "title": title,
            "x_axis_title": x_axis_title,
            "y_axis_title": y_axis_title,
            "auto_axis": auto_axis,
            "y_axis_limits": y_axis_limits,
            "x_axis_limits": x_axis_limits,
            "outdir": str(outdir),
            "export_format": "svg",
            "paths": paths,
            "prism_log": prism_log,
            "pptx_image_source": pptx_image_source,
            "svg_titles_rendered": (
                (
                    "SVG title overlay applied." in prism_log
                    or "Native SVG titles verified." in prism_log
                )
                if run_prism
                else None
            ),
        }

        content: list[dict[str, str]] = [
            {"type": "text", "text": json.dumps(summary, indent=2, ensure_ascii=False)}
        ]
        if return_image_data and svg_exists:
            image = maybe_image_content(Path(paths.get("preview_png") or paths["svg"]))
            if image:
                content.append(image)
        return content
    except Exception:
        if auto_created_outdir and outdir is not None:
            try:
                outdir.rmdir()
            except OSError:
                pass
        raise
    finally:
        cleanup_inline_data(args, data_path)


def tool_prism_preview_template(args: dict[str, Any]) -> list[dict[str, str]]:
    RUNTIME_STATE.assert_current()
    name = slug(str(args.get("name") or "prism_preview"))
    title = str(args.get("title") or name)
    template = str(args["template"])
    hint = str(args["hint"])
    run_prism = bool(args.get("run_prism", True))
    close_prism = bool(args.get("close_prism", True))
    timeout = int(args.get("timeout") or 120)
    auto_axis = bool(args.get("auto_axis", True))
    data_format = str(args.get("data_format") or "wide_numeric")
    first_row_is_data = (
        args.get("first_row_is_data") if "first_row_is_data" in args else None
    )
    data_path = resolve_data_path(args, name)
    outdir: Path | None = None
    auto_created_outdir = False
    try:
        palette, style, title_mode = drawing_style(args, data_path)
        if args.get("outdir"):
            outdir = Path(str(args["outdir"])).expanduser().resolve()
        else:
            DEFAULT_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
            outdir = Path(tempfile.mkdtemp(prefix=f"{name}_", dir=DEFAULT_OUTPUT_ROOT))
            auto_created_outdir = True

        template_path = bridge.resolve_template(template)
        project = outdir / f"{name}.pzfx"
        patch_mode, entry = bridge.build_unverified_preview_project(
            template_path,
            data_path,
            project,
            title,
            hint=hint,
            first_row_is_data=first_row_is_data,
            data_format=data_format,
        )
        if palette.get("group_colors") is not None:
            palette = bridge.template_styles.apply_colors(
                project, entry["alias"], palette["group_order"], palette["group_colors"]
            )
        if data_format == "long_twoway":
            default_x_title, default_y_title = None, "Value"
        else:
            default_x_title, default_y_title = bridge.default_axis_titles(
                data_path, first_row_is_data, hint
            )
        x_axis_title = (
            str(args["x_axis_title"]) if "x_axis_title" in args else default_x_title
        )
        y_axis_title = (
            str(args["y_axis_title"]) if "y_axis_title" in args else default_y_title
        )
        if auto_axis:
            y_axis_limits = (
                bridge.long_twoway_axis_limits(data_path)
                if data_format == "long_twoway"
                else bridge.numeric_axis_limits(
                    bridge.read_table(data_path),
                    first_row_is_data,
                    has_x_column=default_x_title is not None,
                )
            )
        else:
            y_axis_limits = None
        script = bridge.create_export_script(
            project,
            outdir,
            name,
            keep_prism_warm=not close_prism,
            graph_title=title,
            x_axis_title=x_axis_title,
            y_axis_title=y_axis_title,
            y_axis_limits=y_axis_limits,
            **style,
        )
        paths = {"project": str(project), "export_script": str(script)}
        render_ok: bool | None = None
        prism_log = ""
        if run_prism:
            render_ok, prism_log, exported_svg = bridge.run_prism_export_staged(
                project,
                outdir,
                name,
                timeout=timeout,
                keep_prism_warm=not close_prism,
                graph_title=title,
                x_axis_title=x_axis_title,
                y_axis_title=y_axis_title,
                y_axis_limits=y_axis_limits,
                title_mode=title_mode,
                **style,
            )
            if render_ok and exported_svg.exists():
                if palette.get("group_colors") and not set(
                    palette["group_colors"].values()
                ).issubset(bridge.svg_render.inspect(exported_svg)["colors"]):
                    raise ValueError("Prism did not render the requested group colors")
                paths["svg"] = str(exported_svg)

        summary = {
            "native_style": palette,
            "graph_index": style["graph_index"],
            "source_matches_export": (
                "Native SVG titles verified." in prism_log if run_prism else None
            ),
            "ok": False,
            "render_ok": render_ok,
            "automation_verified": False,
            "safe_for_publication": False,
            "requires_manual_review": True,
            "warning": (
                "Experimental template preview: graph bindings, axes, labels, statistics, and "
                "series styling have not been verified for the supplied data."
            ),
            "template": entry.get("alias"),
            "template_name": entry.get("name"),
            "hint": hint,
            "data_format": data_format,
            "x_axis_title": x_axis_title,
            "y_axis_title": y_axis_title,
            "auto_axis": auto_axis,
            "y_axis_limits": y_axis_limits,
            "patch_mode": patch_mode,
            "outdir": str(outdir),
            "paths": paths,
            "prism_log": prism_log,
            "svg_titles_rendered": (
                (
                    "SVG title overlay applied." in prism_log
                    or "Native SVG titles verified." in prism_log
                )
                if run_prism
                else None
            ),
        }
        return text_content(json.dumps(summary, indent=2, ensure_ascii=False))
    except Exception:
        if auto_created_outdir and outdir is not None:
            try:
                outdir.rmdir()
            except OSError:
                pass
        raise
    finally:
        cleanup_inline_data(args, data_path)


def tool_windows_library(action: str, args: dict[str, Any]) -> list[dict[str, str]]:
    # A fresh child is bound to the parent release; the MCP stdin is never
    # inherited. JSON input is an explicit new pipe, not producer code.
    cmd=[PYTHON,str(ROOT/'runtime_state.py'),'--root',str(SKILL_ROOT),
         '--expected-identity',RUNTIME_STATE.status()['loaded_identity'],'--execute','windows_library',action]
    result=subprocess.run(cmd,input=json.dumps(args,ensure_ascii=False,allow_nan=False),text=True,
                          stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=900 if action=='run'else 60,
                          close_fds=True)
    if result.returncode:raise ValueError(result.stderr[-1800:]or result.stdout[-1800:])
    # Do not return full requests/data arrays or mutable output logs.
    return text_content(json.dumps(json.loads(result.stdout),ensure_ascii=False))


def call_tool(name: str, args: Any, *, context=None) -> list[dict[str, str]]:
    context = context if context is not None else {}
    context['phase'] = 'runtime'
    if name != 'prism_env':
        RUNTIME_STATE.assert_current()
    context['phase'] = 'arguments'
    args = validate_tool_arguments(name, args)
    context['phase'] = 'environment'
    if name in('prism_draw','prism_redraw','prism_export_project','prism_preview_template','prism_list_palettes','prism_windows_run'):
        bridge._require_native_execution()
    context['phase'] = 'execute'
    if name not in error_receipts.OBSERVATION_TOOLS:
        context['before'] = error_receipts.snapshot(bridge)
    if name in('prism_windows_catalog','prism_windows_scope','prism_windows_blueprint','prism_windows_prepare','prism_windows_run'):
        return tool_windows_library(name.removeprefix('prism_windows_'),args)
    if name == "prism_env":
        return tool_prism_env(args)
    if name == 'prism_recover_execution':
        return text_content(json.dumps(bridge.recover_execution(args['operation_id']),ensure_ascii=False))
    if name == "prism_list_templates":
        return tool_prism_list_templates(args)
    if name == "prism_list_palettes":
        return text_content(
            json.dumps(
                bridge.native_palette.list_palettes(), ensure_ascii=False, indent=2
            )
        )
    if name == "prism_inspect_template":
        import native_inventory
        entry = published_entry(args['template'])
        path = SKILL_ROOT / entry['seed'] if entry else bridge.resolve_template(args['template'])
        result = native_inventory.inventory(path)
        result["template"] = args["template"]
        if entry:
            result.update(capabilities=entry['capabilities'], automation_status='published_inherited',
                          template_sha256=entry['seed_sha256'])
        return text_content(json.dumps(result, ensure_ascii=False, indent=2))
    if name == "prism_template_thumbnail":
        import template_gallery
        entry = published_entry(args['template'])
        if entry:
            if args.get('refresh') or args.get('graph_index', 1) != 1:
                raise ValueError('Published inherited thumbnail is the approved single-graph reference; refresh requires revalidation')
            png = (SKILL_ROOT / entry['seed']).with_suffix('.png')
            image = maybe_image_content(png)
            if image is None:
                raise ValueError('Published template reference preview is missing')
            return text_content(json.dumps({'template': entry['alias'], 'png': str(png), 'reference_preview': True,
                                            'capabilities': entry['capabilities'], 'automation_status': 'published_inherited',
                                            'template_sha256': entry['seed_sha256']}, ensure_ascii=False)) + [image]
        result = template_gallery.thumbnail(
            args["template"],
            args.get("graph_index", 1),
            args.get("refresh", False),
            args.get("timeout", 25),
        )
        return text_content(json.dumps(result, ensure_ascii=False)) + [
            maybe_image_content(Path(result["png"]))
        ]
    if name == "prism_redraw":
        spec = json.loads(Path(args["spec"]).expanduser().read_text())
        if spec.get("version") != 1 or spec.get("tool") != "prism_draw":
            raise ValueError("Not a supported Prism figure specification")
        options = dict(spec["arguments"])
        if not isinstance(spec.get("template_sha256"), str) or not re.fullmatch(
            "[0-9a-f]{64}", spec["template_sha256"]
        ):
            raise ValueError(
                "Figure specification is missing its required template fingerprint"
            )
        entry = published_entry(options['template'])
        if entry and 'graph_title' in options:
            options['title'] = options.pop('graph_title')
        seed = SKILL_ROOT / entry['seed'] if entry else bridge.resolve_template(options['template'])
        if (
            hashlib.sha256(
                seed.read_bytes()
            ).hexdigest()
            != spec["template_sha256"]
        ):
            raise ValueError(
                "The template changed since this figure was created; choose the current template explicitly"
            )
        options.pop("outdir", None)
        if "palette" in args and "group_colors" not in args:
            options.pop("group_colors", None)
        options.update({k: v for k, v in args.items() if k != "spec"})
        if "data" in args:
            options["data_is_path"] = args.get("data_is_path", False)
        return call_tool("prism_draw", options)
    if name == "prism_export_project":
        source = Path(args["project"]).expanduser().resolve()
        if not source.is_file() or source.suffix.lower() not in {
            ".pzfx",
            ".pzf",
            ".prism",
            ".pzt",
        }:
            raise ValueError("Expected an existing Prism project")
        DEFAULT_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        output = (
            Path(args["outdir"]).expanduser().resolve()
            if args.get("outdir")
            else Path(tempfile.mkdtemp(prefix="prism_export_", dir=DEFAULT_OUTPUT_ROOT))
        )
        output.mkdir(parents=True, exist_ok=True)
        copied = output / source.name
        if copied == source or copied.exists():
            raise ValueError("Use a new output directory to preserve existing projects")
        shutil.copy2(source, copied)
        base = slug(args.get("name") or source.stem)
        ok, log, svg = bridge.run_prism_export_staged(
            copied,
            output,
            base,
            graph_index=args.get("graph_index", 1),
            timeout=args.get("timeout", 120),
            clean_metadata=False,
        )
        result = {
            "ok": ok,
            "data_revalidated": False,
            "project": str(copied),
            "log": log,
        }
        if ok:
            png = svg.with_suffix(".png")
            bridge.svg_render.render(svg, png)
            result.update(svg=str(svg), preview_png=str(png))
        return text_content(json.dumps(result, ensure_ascii=False))
    if name == "prism_list_template_catalog":
        return tool_prism_list_template_catalog(args)
    if name == "prism_match_template":
        return tool_prism_match_template(args)
    if name == "prism_draw":
        return tool_prism_draw(args)
    if name == "prism_preview_template":
        return tool_prism_preview_template(args)
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
                "serverInfo": {"name": "prism-mcp-bridge", "version": SERVER_VERSION},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        context = {'phase': 'arguments'}
        name = ''
        try:
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise McpError('Tool params must be a JSON object')
            name = params.get('name')
            if not isinstance(name, str):
                raise McpError('Tool name must be a string')
            content = call_tool(name, params.get('arguments', {}), context=context)
            failure = error_receipts.native_failure(content, name)
            if failure is not None:
                message = failure.get('prism_log') or failure.get('log') or 'Native execution reported failure'
                receipt = error_receipts.make_receipt(
                    RuntimeError(str(message)), tool=name, phase=context['phase'],
                    before=context.get('before'), after=error_receipts.snapshot(bridge))
                content = [dict(content[0], text=json.dumps(dict(failure, **receipt), ensure_ascii=False)), *content[1:]]
            # Returned summaries retain their legacy isError and native result
            # flags; explicit native failures also carry the new receipt fields.
            return {"jsonrpc": "2.0", "id": request_id,
                    "result": {"content": content, "isError": False}}
        except Exception as exc:
            receipt = error_receipts.make_receipt(
                exc, tool=name, phase=context['phase'], before=context.get('before'),
                after=error_receipts.snapshot(bridge) if context['phase'] == 'execute' and name not in error_receipts.OBSERVATION_TOOLS else None,
                invalid_arguments=isinstance(exc, McpError),
                runtime_status=exc.status if isinstance(exc, RuntimeRestartRequired) else None)
            if isinstance(exc, McpError):
                receipt['mcp_error_code'] = exc.code
            return {"jsonrpc": "2.0", "id": request_id,
                    "result": {"content": text_content(json.dumps(receipt, ensure_ascii=False)), "isError": True}}
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> int:
    removed = cleanup_stale_inline_data()
    if removed:
        log(
            f"removed {removed} stale inline-data director{'y' if removed == 1 else 'ies'}"
        )
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
            send(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": str(exc)},
                }
            )
        except Exception as exc:
            log(f"unhandled error: {exc}")
            send(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32000, "message": str(exc)},
                }
            )
    log("server stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
