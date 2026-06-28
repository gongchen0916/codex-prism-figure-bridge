# Codex Prism Figure Bridge

Codex skill and helper scripts for building GraphPad Prism figures from data, exporting publication-friendly vector artwork, and keeping PowerPoint or Adobe Illustrator documents connected to the underlying Prism source file on macOS.

This repository is intentionally practical rather than magical. macOS does not provide Windows-style editable Prism OLE embedding inside PowerPoint or Illustrator. The bridge uses stable file-based links:

- Prism `.pzfx` remains the editable scientific source.
- Prism command scripts export SVG/PDF/TIF/EPS to predictable paths.
- PowerPoint or Illustrator links to the exported artwork.
- A watcher or refresh script re-exports from Prism and updates the linked artwork.

## What This Solves

- Generate Prism `.pzfx` files from CSV/TSV data using reusable templates.
- Export Prism graphs through command files without clicking the GUI.
- Package a PowerPoint slide where the figure links back to the Prism source.
- Build an Illustrator `.ai` file with a linked Prism-exported PDF.
- Refresh Illustrator after the Prism source is modified.
- Maintain a local template catalog for fast template matching.

## Current Limitations

- macOS PowerPoint does not support true Windows OLE Prism objects.
- Illustrator cannot link directly to `.pzfx`; it links to a Prism-exported PDF/EPS/SVG.
- In testing, Illustrator scripting could open SVG as editable artwork, but could not reliably create SVG as a linked `placedItem`. PDF worked as the linked update target.
- Exported PDF/EPS/SVG files are artwork, not live Prism objects.
- Downloaded third-party Prism templates are not included by default. Keep only templates you are licensed to redistribute.

## Repository Layout

```text
SKILL.md                         Codex skill instructions
scripts/                         Prism MCP server and CLI bridge
references/                      Notes on Prism/PPT workflow and template handling
assets/templates/                Small curated template metadata
examples/ppt-linked/             PowerPoint refresh/link helper scripts
examples/illustrator-linked/     Illustrator linked-PDF workflow
```

## Install As A Codex Skill

Clone this repository into your Codex skills folder:

```bash
mkdir -p ~/.codex/skills
git clone https://github.com/YOUR_USER/codex-prism-figure-bridge.git \
  ~/.codex/skills/codex-prism-figure-bridge
```

Then install the MCP bridge:

```bash
cd ~/.codex/skills/codex-prism-figure-bridge
python3 scripts/install_mcp.py
```

## Basic CLI Usage

```bash
python3 scripts/prism_bridge.py env
python3 scripts/prism_bridge.py list-templates
python3 scripts/prism_bridge.py build \
  --data raw.csv \
  --template column-scatter \
  --outdir outputs/figure_1 \
  --name figure_1 \
  --title "Figure 1"
```

To export through Prism:

```bash
python3 scripts/prism_bridge.py build \
  --data raw.csv \
  --template column-scatter \
  --outdir outputs/figure_1 \
  --name figure_1 \
  --title "Figure 1" \
  --run-prism
```

On macOS, Prism command files should be opened by file association:

```bash
open outputs/figure_1/figure_1_export.pzc
```

In local testing, `open -a "Prism 11" file.pzc` launched Prism but did not execute the command file.

## PowerPoint Workflow

The PowerPoint workflow uses linked or replaceable exported artwork plus a `prismbridge://` URL back to the `.pzfx` file. The practical chain is:

```text
Prism .pzfx -> Prism export -> PPT figure image -> prismbridge URL -> Prism source
```

See:

- `examples/ppt-linked/prism_ppt_sync.py`
- `examples/ppt-linked/prismbridge_url_handler.py`
- `references/prism-ppt-flow.md`

## Illustrator Workflow

The Illustrator workflow uses a linked PDF exported from Prism:

```text
Prism .pzfx -> linked_prism_export.pdf -> Illustrator placed item
```

Files:

- `examples/illustrator-linked/create_linked_ai.jsx`
- `examples/illustrator-linked/refresh_prism_to_ai.py`
- `examples/illustrator-linked/watch_prism_ai_link.py`
- `examples/illustrator-linked/open_prism_source_from_ai.jsx`

The AI placed item stores the Prism source path in `note` and `URL` metadata. The refresh script:

1. Opens a Prism `.pzc` command file.
2. Re-exports PDF/SVG/EPS.
3. Normalizes Prism's numbered exports back to the stable linked filename.
4. Refreshes the Illustrator placed item.
5. Saves the `.ai` document.

## Security Notes

This repository should not contain:

- API keys or tokens
- personal absolute paths
- patient or unpublished experimental data
- large generated PPT/AI/PDF/TIF outputs
- proprietary Prism templates unless redistribution is permitted

Before publishing changes:

```bash
rg -n --hidden -S '(sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|OPENAI_API_KEY|ANTHROPIC_API_KEY|api[_-]?key|secret|password|token|/Users/)' .
```

## License

MIT for the bridge code. Prism itself and any Prism templates remain subject to their own licenses.
