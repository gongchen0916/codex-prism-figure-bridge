"""Escaped static evidence reports. Rendering is never counted as approval."""
from html import escape
import json
from pathlib import Path
from urllib.parse import quote


def render_html(manifest):
    cases = manifest.get('cases', [])
    attempted = sum(bool(c.get('calls')) for c in cases)
    blocked = sum(c.get('status') == 'blocked' for c in cases)
    pending = sum(c.get('status') == 'review_required' for c in cases)
    # This runner deliberately has no approval/publishing mechanism.
    parts = ['<!doctype html><html lang="en"><meta charset="utf-8">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\' data: file:; style-src \'unsafe-inline\'">',
             '<title>Prism batch evidence</title><style>body{font:16px system-ui;margin:32px;max-width:1500px}section{border-top:1px solid #bbb;margin-top:24px;padding-top:16px}.previews{display:flex;gap:16px;flex-wrap:wrap}figure{margin:0;width:30%;min-width:260px}img{max-width:100%;border:1px solid #ddd}td,th{text-align:left;padding:4px 12px}pre{white-space:pre-wrap;overflow-wrap:anywhere}.unknown{color:#865500}</style>',
             '<h1>Prism batch evidence</h1><p>Software test data. Source previews are template examples; none of these images establish experimental conclusions.</p>',
             '<p>Attempted: %d · Blocked: %d · Review required: %d · Approved: 0</p>' % (attempted, blocked, pending),
             '<p>Native payload bytes preserved before Prism is a mechanical check. Visual style, data-to-graph binding, semantics and publication approval require separate review.</p>']
    for case in cases:
        parts += ['<section><h2>%s</h2><p>Status: %s</p>' % (escape(str(case.get('id', 'unknown'))), escape(str(case.get('status', 'unknown')))), '<div class="previews">']
        for stage in ('reference', 'new', 'reopened'):
            parts.append('<figure><figcaption>%s · software fixture</figcaption>' % escape(stage))
            previews = case.get('previews', {}).get(stage, {})
            png = previews.get('png')
            svg = previews.get('svg')
            if png:
                parts.append('<img alt="%s software test data" src="%s">' % (escape(stage), escape(quote(str(png), safe='/.'), quote=True)))
            elif svg:
                parts.append('<img alt="%s software test data, SVG preview" src="%s">' % (escape(stage), escape(quote(str(svg), safe='/.'), quote=True)))
            else:
                parts.append('<p class="unknown">Preview unavailable</p>')
            parts.append('</figure>')
        parts += ['</div><table><tr><th>Gate</th><th>State</th></tr>']
        for name, value in case.get('gates', {}).items():
            state = 'PASS' if value is True else 'FAIL / not approved' if value is False else 'UNKNOWN'
            parts.append('<tr><td>%s</td><td>%s</td></tr>' % (escape(name), state))
        parts += ['</table><pre>%s</pre></section>' % escape(json.dumps({k: case.get(k) for k in ('error', 'review_required', 'calls', 'identity', 'attempt_dir')}, indent=2, ensure_ascii=False))]
    return '\n'.join(parts) + '</html>'


def write_report(manifest, path):
    Path(path).write_text(render_html(manifest), encoding='utf-8')
