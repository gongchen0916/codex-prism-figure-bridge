# Prism To PPT Flow

Use the MCP server in `scripts/prism_mcp_server.py`.

The high-level flow is:

1. Call `prism_match_template` to pick the closest whitelisted template alias.
2. Generate a modified Prism `.pzfx` source beside the output figure.
3. Launch a Prism `.pzc` export script through `open -a "Prism 11"` on macOS.
4. Export Prism-rendered **SVG** only.
5. Return the SVG as MCP image content when requested.
6. Optionally create a standard PowerPoint `.pptx` with the SVG embedded and a
   hyperlink back to the `.pzfx` source file.

Mac limitation: PowerPoint for Mac cannot create a native editable Prism OLE
object. The supported Mac workflow is SVG-in-PPT plus file hyperlink back to
the editable Prism source.

Windows upgrade path: use OLE/ActiveX only on Windows if true double-click
editable Prism objects are required.

Performance tips:

- Use `run_prism: false` while iterating on template/data choices.
- Keep `close_prism: false` during batch runs so Prism stays warm.
- Do not use GUI click automation to press Prism's Run button.
