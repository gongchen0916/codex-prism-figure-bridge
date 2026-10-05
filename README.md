# Codex Prism Figure Bridge

MCP server and Python helpers for GraphPad Prism. This update publishes the **0.9.0 MCP core source**, with Windows Prism execution, PowerPoint COM/OLE support, persistent execution records, and runtime identity checks.

This is a **code-only distribution**. It does not include the local template library, source-specific adapters, scientific data, previews, acceptance records, or machine configuration. A fresh clone can start the MCP server and inspect its tools; drawing requires separately provisioned templates and a configured, validated Windows execution environment.

## Current execution model

The host runs on macOS and communicates with a Windows 11 VM through Parallels. The Windows worker invokes Prism's native command-file interface. PowerPoint packaging uses Windows COM to create editable Prism OLE objects. The current backend does not use OpenAI Computer Use or macOS Accessibility to draw figures.

- MCP uses local stdio, with a fixed tool catalog.
- `prism_env` with `runtime_only: true` checks loaded code and execution records without launching Prism or probing the VM.
- Health results distinguish runtime, transport, configuration, native/PPT queues, VM, executor, application processes, OLE, and the UI input route. Unchecked layers remain explicitly unverified.
- A caller timeout does not cancel native execution. An uncertain operation blocks further dispatch until its exact operation ID is reconciled.
- Recovery does not kill Prism, close user documents, or replay the original command automatically.
- Code changes require reconnecting the MCP server. Runtime identity checks prevent a long-lived server from silently mixing releases.

## Included and excluded files

`MCP_SOURCE_MANIFEST.json` lists the 27 current runtime source files and their SHA-256 hashes. Those files are unchanged copies of the local MCP implementation. Tests and documentation are distributed separately from that source manifest.

Template selection and data-replacement logic are code, and are included. Actual templates, catalogs, palettes, native project files, and their private adapters are not part of this update. `prism_windows_*` library tools remain advertised but require that separate library installation; this repository alone does not supply their private dependencies or acceptance evidence.

## Requirements and registration

Use Python 3.10 or newer on the Mac for this source distribution. Native execution additionally requires Parallels, a configured Windows 11 VM, licensed Windows Prism and PowerPoint, and a Windows Python environment with `pywin32` for COM operations.

Clone the source, then register the MCP server:

```bash
git clone https://github.com/gongchen0916/codex-prism-figure-bridge.git
cd codex-prism-figure-bridge
python3 scripts/install_mcp.py
```

The installer preserves an existing matching registration and reports a conflict instead of replacing a different registration. It does not install or activate Prism, create a VM, or provision a template library.

Optional SVG preview dependencies are checked or installed separately:

```bash
python3 scripts/setup_render.py --check
python3 scripts/setup_render.py
```

Machine configuration belongs in the ignored local file `assets/prism_execution.json`. It must identify the intended VM, shared folder, Windows executables, and Prism executable fingerprint. The executor validates its schema and acceptance phase before native dispatch. Missing configuration leaves execution unavailable; it does not enable a macOS fallback. See `scripts/windows_prism_executor.py` for the policy contract. Do not label an environment accepted before its native save/reopen and OLE behavior has been verified.

## Verification and recovery

Run the isolated tests without starting Prism or PowerPoint:

```bash
python3 -B -m unittest discover -s tests -q
```

These tests cover runtime identity, registration, health reporting, execution policy, timeout fencing, recovery, and the source-only MCP protocol. They are not native figure acceptance.

After reconnecting a configured MCP, call `prism_env` with `runtime_only: true`. Require equal loaded/disk identities and `restart_required: false`. For an uncertain operation, read its operation ID and call `prism_recover_execution` with that ID. Inspect original outputs when late completion is confirmed; do not repeat the original drawing command merely because the client timed out.

A completed command or a running process does not establish scientific correctness, OLE responsiveness, or figure quality. Native deliveries still require saved-data checks, save/reopen verification, visual inspection, and representative OLE activation at the intended physical size.

## Security and license

Do not commit credentials, app registrations, local execution policies, template libraries, scientific data, or generated outputs. New ignore rules prevent those files from entering routine commits; review the staged file list before every push.

The bridge code is MIT licensed. Prism, PowerPoint, and separately installed templates retain their respective licenses.
