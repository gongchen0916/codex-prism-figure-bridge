---
name: codex-prism-figure-bridge
description: Use the local Prism MCP for template-based figures, native Windows Prism execution, and editable Prism OLE PowerPoint output. Templates and machine configuration are installed separately.
---

# Codex Prism Figure Bridge

This repository distributes MCP core source. Read README.md for the current Windows execution backend, dependencies, and the boundary between included code and the separately installed private template library.

## Runtime checks

Call `prism_env` with `runtime_only: true` to inspect loaded code and execution records without launching applications. Require `runtime.restart_required: false` and equal loaded/disk identities after code updates. Reconnect the MCP server when code is stale; do not restart Prism to reload Python code.

Keep runtime readiness separate from native correctness. A responding tool or a running Prism process does not prove that an operation succeeded or a saved figure is valid.

## Figure requests

Use available catalog and matching tools to identify an exact locally installed template before drawing. Preserve the requested data mapping, uncertainty definition, graph type, and native style. If the local library or source-specific adapter is missing, report that dependency; do not substitute a guessed template or manufacture acceptance records.

Use the Windows native execution route for supported drawing and export operations. For editable PowerPoint delivery, require a real Windows Prism OLE object, preserve native physical size and aspect ratio, and verify save/reopen plus representative object activation. Screenshots and linked pictures do not establish native editability.

## Uncertain execution

A timeout is not cancellation. Read the recorded operation ID before another dispatch. Use `prism_recover_execution` only for that exact operation. Late completion requires inspecting the original outputs. Unknown execution stays blocked; never delete journals, replay a potentially completed action, or force-quit user applications to clear the state.

## Source-only maintenance

Keep template files, catalogs, source-specific private adapters, data, native acceptance evidence, local configuration, and generated outputs outside Git commits. The source manifest records included runtime bytes; it is not native acceptance. Run isolated tests and inspect the actual staged diff before publishing MCP updates.
