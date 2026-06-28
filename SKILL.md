---
name: codex-prism-figure-bridge
description: Build GraphPad Prism-rendered scientific figures from CSV/TSV data using a bundled Prism template library, return rendered SVG through MCP, and package figures into PowerPoint slides that hyperlink back to editable Prism source files. Use when Codex needs Prism automation, Prism templates, Prism-to-PPT linking, or reproducible biological/scientific plots with GraphPad Prism.
---

# Codex Prism Figure Bridge

Use this skill when a user wants Prism itself to render the figure, not a
matplotlib imitation. Prefer it for scientific plots, Prism templates, Prism
automation, PPT figure packaging, or reusable figure workflows.

## Core Rules

- Use MCP/file automation first. Never use System Events or screenshot clicking
  to press Prism UI buttons.
- Default export format is **SVG only** (not PNG).
- Do not rewrite whole Prism XML files through generic XML serializers when a
  template-preserving column patch is available; preserve template text and
  replace only title and `YColumn` blocks.
- On macOS, PowerPoint cannot create native editable Prism OLE objects. Package a
  Prism-rendered SVG in PPTX and hyperlink it back to the `.pzfx` source. Use
  Windows OLE only when true double-click editable objects are required.
- Keep the Prism source, export script, SVG, and PPTX together in the output
  folder.
- Prefer portfolio templates from the automation whitelist in
  `assets/templates/curated_templates.json`.

## Two-Phase Workflow

1. Inspect the user data shape and any reference image or figure description.
2. If MCP server `prism` is not registered, run:

```bash
python3 scripts/install_mcp.py
```

3. Call `prism_match_template` first:

```json
{
  "data": "Control,Treated\n1.0,2.0\n1.2,2.4\n0.9,2.1",
  "hint": "column scatter plot with individual points"
}
```

4. Iterate quickly with `run_prism: false` until template and data look right:

```json
{
  "data": "Control,Treated\n1.0,2.0\n1.2,2.4\n0.9,2.1",
  "template": "column-scatter",
  "name": "figure_1",
  "title": "Figure 1",
  "run_prism": false
}
```

5. Final render with SVG export:

```json
{
  "data": "Control,Treated\n1.0,2.0\n1.2,2.4\n0.9,2.1",
  "template": "column-scatter",
  "name": "figure_1",
  "title": "Figure 1",
  "run_prism": true,
  "close_prism": false,
  "return_image_data": true,
  "make_pptx": true
}
```

6. Return the `.pzfx`, `_export.pzc`, `.svg`, and optional `.pptx` paths.

## MCP Tools

- `prism_env`: local Prism/PowerPoint capability check
- `prism_list_templates`: automation-whitelisted template aliases
- `prism_match_template`: rank templates from data shape + hints
- `prism_draw`: build `.pzfx`, optionally export SVG through Prism

## CLI Fallback

```bash
python3 scripts/prism_bridge.py env
python3 scripts/prism_bridge.py list-templates
python3 scripts/prism_bridge.py build \
  --data raw.tsv \
  --template column-scatter \
  --outdir outputs/my_figure \
  --name figure_1 \
  --title "Figure 1"
python3 scripts/prism_bridge.py build \
  --data raw.tsv \
  --template column-scatter \
  --outdir outputs/my_figure \
  --name figure_1 \
  --title "Figure 1" \
  --run-prism
```

## Resources

- `scripts/prism_mcp_server.py`: stdio MCP server
- `scripts/prism_bridge.py`: shared build/export library
- `scripts/template_matcher.py`: data-shape template ranking
- `scripts/install_mcp.py`: registers the bundled MCP server with Codex as `prism`
- `assets/templates/curated_templates.json`: figure-type mappings + automation whitelist
- `assets/templates/template_index.json`: full searchable template catalog
- `references/template-library.md`: template source, licensing, patcher notes
- `references/prism-ppt-flow.md`: Prism-to-PPT link workflow

## Current Automation Scope

The bundled library contains 563 Prism files: the original curated/portfolio
templates plus 312 user-downloaded unique templates in
`assets/templates/user_downloaded_312`. Automated patching is limited to the
whitelist in `curated_templates.json` (portfolio XML templates such as
`column-scatter`, `box-and-whiskers-with-asterisks`, `theoretical-curves`).
Column templates use template-preserving patching; XY templates use a narrow
ElementTree patcher. Downloaded/binary templates are indexed for matching and
reference but are not auto-patched unless the user supplies a compatible XML
template path.

## Performance Notes

- Mac automation opens `.pzc` through Prism directly; do not click Run via UI automation.
- Leave `close_prism: false` during batch runs so Prism stays warm between exports.
- First Prism launch may be slow until the Welcome dialog is dismissed once.
