# Template Library

The bundled templates come from two sources.

Official Portfolio templates come from the local GraphPad Prism 11 installation:

`/Applications/Prism 11.app/Contents/SharedSupport/Portfolio`

GraphPad documents Portfolio graphs as examples that can be opened, explored,
edited, and reused as graph starting points. The skill copies these installed
Portfolio files into `assets/templates/portfolio/` and indexes them in
`assets/templates/template_index.json`.

Do not bulk-download random `.pzt`, `.pzfx`, or `.prism` files unless their
license permits redistribution. Prefer official GraphPad Portfolio/example files
or user-provided lab templates.

User-downloaded templates are stored in `assets/templates/downloaded/`. They
were imported with SHA-256 de-duplication and validated by opening them in Prism
and exporting a preview SVG.

## Template Selection

1. Call MCP tool `prism_match_template` with the data and a short figure hint.
2. Prefer matches from `assets/templates/curated_templates.json`.
3. Restrict automated builds to the whitelist in that file unless the user
   explicitly provides a compatible XML template path.

Useful whitelist aliases include:

- `column-scatter`
- `box-and-whiskers-graph`
- `box-and-whiskers-with-asterisks`
- `scatter-plot-with-bars`
- `before-after`
- `before-after-with-error`
- `volcano-plot`
- `spaghetti-plot`
- `dose-response-curves` (zip bundle; open manually in Prism)
- `theoretical-curves`
- `odds-ratio-forest-plot`

## Patcher Behavior

- Column templates without `XColumn`: template-preserving text patch of title and
  `YColumn` blocks.
- XY templates with `XColumn`: narrow ElementTree data replacement.
- Binary `.pzf` and zip-bundle templates: not auto-patched.
- Export format: SVG only.
