"""Render Prism SVG with positioned text; never fall back to macOS sips."""

from pathlib import Path
import os
import subprocess
import sys
import re
import base64
import struct
import tempfile
import json
import importlib.util
from xml.etree import ElementTree as ET


def _isolated_render(svg, png, scale, requirement):
    runtime = Path(os.environ.get("PRISM_RENDER_PYTHON")
                   or Path(__file__).resolve().parents[1] / ".venv/bin/python")
    if os.environ.get("PRISM_RENDER_DELEGATED") or not runtime.is_file():
        raise RuntimeError(requirement)
    png.parent.mkdir(parents=True, exist_ok=True)
    # A pre-existing cache PNG must never satisfy a child invocation that did
    # not produce a new image. Keep the previous cache untouched on failure.
    with tempfile.TemporaryDirectory(prefix=".prism-render-", dir=png.parent) as directory:
        fresh = Path(directory) / "preview.png"
        cp = subprocess.run(
            [
                str(runtime),
                str(Path(__file__).resolve()),
                str(svg),
                str(fresh),
                str(scale),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "PRISM_RENDER_DELEGATED": "1"},
        )
        if cp.returncode or not fresh.is_file() or not fresh.stat().st_size:
            raise RuntimeError("SVG preview render failed: " + (cp.stderr or cp.stdout))
        fresh.replace(png)


def _gradient_fragment_references(root):
    # resvg can resolve local image paths that the prior renderer ignored.
    # Native plots use local IDs and some base64 JPEG backgrounds. Raster data
    # cannot contain nested SVG hrefs; check both its MIME and binary signature.
    for node in root.iter():
        values = list(node.attrib.values())
        for key, value in node.attrib.items():
            if key.rsplit('}', 1)[-1] == 'href' and not re.fullmatch(r'#[^\s]+', value.strip()):
                embedded = re.fullmatch(r'data:image/(png|jpeg);base64,(.*)', value.strip(), re.I | re.S)
                raster = False
                if node.tag.rsplit('}', 1)[-1] == 'image' and embedded:
                    try:
                        raw = base64.b64decode(re.sub(r'\s+', '', embedded.group(2)), validate=True)
                        if embedded.group(1).lower() == 'png':
                            raster = len(raw) >= 24 and raw.startswith(b'\x89PNG\r\n\x1a\n')
                        else:
                            raster = len(raw) >= 4 and raw.startswith(b'\xff\xd8\xff') and raw.endswith(b'\xff\xd9')
                    except ValueError:
                        pass
                if not raster:
                    raise ValueError('Gradient previews allow fragment hrefs or validated base64 PNG/JPEG raster images only; external and embedded SVG resources are unsupported')
        if node.tag.rsplit('}', 1)[-1] == 'style':
            values.append(''.join(node.itertext()))
        for value in values:
            if re.search(r'@import\b', value, re.I):
                raise ValueError('Gradient previews allow only same-document fragment resources, not imported CSS')
            for reference in re.findall(r'url\(\s*(.*?)\s*\)', value, re.I):
                if not re.fullmatch(r'#[^\s]+', reference.strip().strip('\"\'')):
                    raise ValueError('Gradient previews allow only same-document fragment URL resources')


def requires_positioned_text_renderer(root):
    """MuPDF substitutes en/em spaces with wrong advances across native runs.

    Keep source SVG untouched. resvg honors its textLength and installed Arial
    metrics, including Prism's separate kerning runs for adjacent digits.
    """
    # Windows Prism exports one x coordinate per glyph. MuPDF can omit these
    # entire runs; resvg respects the positioning without changing the SVG.
    if any(n.tag.rsplit('}',1)[-1]=='tspan' and len(n.get('x','').split())>1 for n in root.iter()):
        return True
    # Limit this automatic switch to the measured native beta-caption
    # dialect. Other en-space labels include older publication templates
    # whose full-ink checks intentionally use the MuPDF backend.
    if 'β' not in ''.join(root.itertext()):
        return False
    return any(n.tag.rsplit('}', 1)[-1] == 'tspan' and n.get('textLength') is not None
               and any(c in ''.join(n.itertext()) for c in ('\u2002', '\u2003'))
               for n in root.iter())


def _native_font_files(root):
    if not any(n.get('font-family')=='Segoe UI Symbol'for n in root.iter()):return []
    path=Path(__file__).with_name('windows_font_cache.py')
    spec=importlib.util.spec_from_file_location('_prism_native_font_cache',path)
    module=importlib.util.module_from_spec(spec)
    exec(compile(path.read_bytes(),str(path),'exec',dont_inherit=True),module.__dict__)
    return module.svg_fonts(root)


def render(svg, png, scale=3):
    svg, png = Path(svg), Path(png)
    root = ET.parse(svg).getroot()
    gradients = any(n.tag.rsplit('}', 1)[-1] in {'linearGradient', 'radialGradient'} for n in root.iter())
    # MuPDF also misinterprets absolute units on nested SVG viewports. Prism
    # layouts have a .75 wrapper which cancels CSS pt-to-px (4/3); rendering
    # them with MuPDF falsely shrinks every panel and its text by 25 percent.
    nested_viewports = any(n is not root and n.tag.rsplit('}', 1)[-1] == 'svg'
                           for n in root.iter())
    accurate_backend = gradients or nested_viewports or requires_positioned_text_renderer(root)
    if accurate_backend:
        _gradient_fragment_references(root)
    try:
        import pymupdf
    except ImportError:
        _isolated_render(svg, png, scale, 'Readable Prism SVG previews require PyMuPDF. Run scripts/setup_render.py with Python 3.10 or newer.')
        return
    if accurate_backend:
        try:
            import resvg_py
        except ImportError:
            _isolated_render(svg, png, scale, 'Gradient and positioned Unicode previews require resvg-py in the isolated renderer. Run scripts/setup_render.py with Python 3.10 or newer.')
            return
    png.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(svg) as document:
        if accurate_backend:
            # MuPDF 1.28.2 silently paints SVG gradients black. Keep its exact
            # output for ordinary Prism plots. resvg's width+height options fit
            # the aspect ratio and can round away one or two pixels. Instead,
            # set an integer viewport only in this in-memory SVG representation.
            rect = document[0].rect
            pixels = (rect * pymupdf.Matrix(scale, scale)).irect
            if pixels.width <= 0 or pixels.height <= 0:
                raise ValueError('Gradient preview requires a positive pixel canvas')
            root.set('width', '%dpx' % pixels.width)
            root.set('height', '%dpx' % pixels.height)
            if 'viewBox' not in root.attrib:
                root.set('viewBox', '0 0 %.12g %.12g' % (rect.width, rect.height))
            data = resvg_py.svg_to_bytes(svg_string=ET.tostring(root, encoding='unicode'),
                background='#ffffff', dpi=96.0, log_information=False,
                font_files=_native_font_files(root),
                style_sheet='[font-family="ArialMT"] { font-family: Arial; } '
                            '[font-family="PingFangSC"] { font-family: "PingFang SC"; }')
            if len(data) < 24 or data[:8] != b'\x89PNG\r\n\x1a\n' or struct.unpack('>II', data[16:24]) != (pixels.width, pixels.height):
                raise RuntimeError('Gradient renderer changed the required native pixel canvas')
            png.write_bytes(data)
        else:
            document[0].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(png)


def inspect(svg):
    root = ET.parse(svg).getroot()
    texts = [
        "".join(n.itertext()) for n in root.iter() if n.tag.rsplit("}", 1)[-1] == "text"
    ]
    colors = sorted(
        {
            v.upper()
            for n in root.iter()
            for k, v in n.attrib.items()
            if k in {"fill", "stroke"} and v.startswith("#")
        }
    )
    return {
        "width": root.get("width"),
        "height": root.get("height"),
        "texts": texts,
        "colors": colors,
    }


def text_runs(svg):
    """Reassemble native positioned glyph runs without joining unrelated labels."""
    root = svg if isinstance(svg, ET.Element) else ET.parse(svg).getroot()
    positioned = {}
    runs = []

    def advance(value, size):
        return (
            sum(
                (
                    0.33
                    if c.isspace()
                    else (
                        0.3
                        if c in "ilI.,:;!|"
                        else (
                            0.83
                            if c in "mM"
                            else (
                                0.94
                                if c in "W@"
                                else (
                                    0.72
                                    if c.isupper() or c == "w"
                                    else 1 if ord(c) > 255 else 0.56
                                )
                            )
                        )
                    )
                )
                for c in value
            )
            * size
        )

    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "text":
            continue
        spans = [n for n in node if n.tag.rsplit("}", 1)[-1] == "tspan"] or [node]
        for span in spans:
            value = "".join(span.itertext())
            x = span.get("x", node.get("x"))
            y = span.get("y", node.get("y"))
            if x is None or y is None:
                runs.append(dict(text=value, baseline=None, orientation=None))
                continue
            try:
                size = float(node.get("font-size", "10"))
                start = float(re.split(r"[ ,]+", x)[0])
                baseline = float(re.split(r"[ ,]+", y)[0])
                width = float(span.get("textLength", str(advance(value, size))))
            except ValueError:
                runs.append(dict(text=value, baseline=None, orientation=None))
                continue
            key = (node.get("transform", ""), round(baseline, 3), size)
            positioned.setdefault(key, []).append((start, start + width, value))
    for (transform, baseline, size), spans in positioned.items():
        numbers = [
            float(x)
            for x in re.findall(r"[-+]?(?:\d*\.?\d+)(?:[eE][-+]?\d+)?", transform)
        ]
        a, b, c, d, e, f = (
            numbers
            if transform.startswith("matrix") and len(numbers) == 6
            else (1, 0, 0, 1, 0, 0)
        )
        horizontal = abs(b) < abs(a) * 0.1
        vertical = abs(a) < abs(b) * 0.1
        current = ""
        end = None
        run_start = None

        def append_run(value, start, stop):
            if not value.strip():
                return
            corners = [
                (a * x + c * y + e, b * x + d * y + f)
                for x in (start, stop)
                for y in (baseline - size, baseline + 0.2 * size)
            ]
            runs.append(
                dict(
                    text=value,
                    baseline=b * (start + stop) / 2 + d * baseline + f,
                    orientation=(
                        "horizontal"
                        if horizontal
                        else "vertical" if vertical else "angled"
                    ),
                    x0=min(x for x, y in corners),
                    x1=max(x for x, y in corners),
                    y0=min(y for x, y in corners),
                    y1=max(y for x, y in corners),
                    font_size=size,
                )
            )

        for start, stop, value in sorted(spans):
            if end is not None and -0.3 * size <= start - end <= 0.75 * size:
                current += value
            else:
                if current:
                    append_run(current, run_start, end)
                current = value
                run_start = start
            end = stop
        if current:
            append_run(current, run_start, end)
    return runs


def _plot_geometry(root):
    """Find native left/bottom axes and legend samples outside clipped data."""
    number = r"[-+]?(?:\d*\.?\d+)(?:[eE][-+]?\d+)?"
    parents = {child: parent for parent in root.iter() for child in parent}
    lines = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "path":
            continue
        color = (node.get("stroke") or "").lower()
        if not color or color in {"none", "white", "#fff", "#ffffff"}:
            continue
        ancestor = node
        clipped = False
        while ancestor in parents:
            ancestor = parents[ancestor]
            if (
                ancestor.get("clip-path")
                or ancestor.tag.rsplit("}", 1)[-1] == "clipPath"
            ):
                clipped = True
                break
        if clipped:
            continue
        path = node.get("d", "")
        if any(
            cmd not in {"M", "L", "Z"}
            for cmd in re.findall(r"[A-Za-z]", re.sub(number, "", path))
        ):
            continue
        transform = node.get("transform", "")
        matrix = [float(v) for v in re.findall(number, transform)]
        if transform and (not transform.startswith("matrix") or len(matrix) != 6):
            continue
        a, b, c, d, e, f = matrix or (1, 0, 0, 1, 0, 0)
        previous = None
        for cmd, x, y in re.findall(
            r"([ML])\s*(" + number + r")[ ,]+(" + number + r")", path
        ):
            x, y = float(x), float(y)
            point = (a * x + c * y + e, b * x + d * y + f)
            if cmd == "L" and previous is not None:
                lines.append((color, previous, point))
            previous = point
    black = {"black", "#000", "#000000"}
    horizontal = [
        (min(p[0], q[0]), max(p[0], q[0]), (p[1] + q[1]) / 2)
        for color, p, q in lines
        if color in black and abs(p[1] - q[1]) < 0.5 and abs(p[0] - q[0]) > 40
    ]
    vertical = [
        ((p[0] + q[0]) / 2, min(p[1], q[1]), max(p[1], q[1]))
        for color, p, q in lines
        if color in black and abs(p[0] - q[0]) < 0.5 and abs(p[1] - q[1]) > 40
    ]
    frames = [
        (left, right, top, bottom)
        for left, right, y in horizontal
        for x, top, bottom in vertical
        if abs(x - left) < 2 and abs(y - bottom) < 2
    ]
    if not frames:
        return None
    left, right, top, bottom = max(frames, key=lambda v: (v[1] - v[0]) * (v[3] - v[2]))
    legend_y = [
        (p[1] + q[1]) / 2
        for _, p, q in lines
        if abs(p[1] - q[1]) < 0.5
        and 6 <= abs(p[0] - q[0]) < 0.4 * (right - left)
        and min(p[1], q[1]) > bottom + 5
    ]
    return dict(
        left=left,
        right=right,
        top=top,
        bottom=bottom,
        legend_top=min(legend_y) if legend_y else None,
    )


def _title_candidates(root, runs, key):
    horizontal = [r for r in runs if r["orientation"] == "horizontal"]
    if key == "graph_title":
        top = min((r["baseline"] for r in horizontal), default=None)
        return [
            r for r in horizontal if top is not None and abs(r["baseline"] - top) < 1
        ]
    if key == "y_axis_title":
        return [r for r in runs if r["orientation"] == "vertical"]
    if key != "x_axis_title":
        return runs
    frame = _plot_geometry(root)
    if frame is None:
        bottom = max((r["baseline"] for r in horizontal), default=None)
        return [
            r
            for r in horizontal
            if bottom is not None and abs(r["baseline"] - bottom) < 1
        ]
    center = (frame["left"] + frame["right"]) / 2
    candidates = []
    for r in horizontal:
        gap = r["baseline"] - frame["bottom"]
        if abs((r["x0"] + r["x1"]) / 2 - center) > max(
            2, 0.1 * (frame["right"] - frame["left"])
        ):
            continue
        if (
            not 1.8 * r["font_size"]
            < gap
            < max(4 * r["font_size"], 0.35 * (frame["bottom"] - frame["top"]))
        ):
            continue
        if frame["legend_top"] is not None and r["y1"] >= frame["legend_top"] - 2:
            continue
        if any(
            other is not r
            and _run_overlap(r,other)
            for other in horizontal
        ):
            continue
        candidates.append(r)
    return candidates


def _run_overlap(left,right):
    # Ink is used only when both runs have complete unique glyph evidence.
    precise='ink_box'in left and 'ink_box'in right
    a=left['ink_box']if precise else(left['x0'],left['y0'],left['x1'],left['y1'])
    b=right['ink_box']if precise else(right['x0'],right['y0'],right['x1'],right['y1'])
    return min(a[2],b[2])>max(a[0],b[0])+.5 and min(a[3],b[3])>max(a[1],b[1])+.5


def _add_positioned_ink(root,runs):
    if not any(node.tag.rsplit('}',1)[-1]=='tspan'and len(node.get('x','').split())>1 for node in root.iter()):return
    text_nodes=[node for node in root.iter()if node.tag.rsplit('}',1)[-1]=='text']
    if any(node.get('font-family')not in('Arial','ArialMT')or not ''.join(node.itertext()).isascii()for node in text_nodes):return
    if any(node.tag.rsplit('}',1)[-1]=='style'and ''.join(node.itertext()).strip()for node in root.iter()):return
    parents={child:parent for parent in root.iter()for child in parent}
    paint_nodes=[node for node in root.iter()if node.tag.rsplit('}',1)[-1]in('text','tspan')]
    for text_node in paint_nodes:
        node=text_node
        while node is not None:
            if node.get('font-family')not in(None,'Arial','ArialMT'):return
            if node.get('font-style')not in(None,'normal'):return
            if node.tag.rsplit('}',1)[-1]=='tspan'and node.get('font-size')is not None:return
            if node.get('fill','black').lower()not in('black','#000','#000000'):return
            if node.get('stroke')not in(None,'none'):return
            if any(key in node.attrib for key in('style','mask','filter','clip-path','display','visibility')):return
            for key in('opacity','fill-opacity'):
                if key in node.attrib:
                    try:
                        if float(node.get(key))!=1:return
                    except ValueError:return
            node=parents.get(node)
    runtime=Path(os.environ.get('PRISM_RENDER_PYTHON')or Path(__file__).resolve().parents[1]/'.venv/bin/python')
    if not runtime.is_file():return
    try:
        result=subprocess.run([str(runtime),str(Path(__file__).with_name('svg_text_ink.py'))],
            input=ET.tostring(root),capture_output=True,timeout=10,close_fds=True)
        if result.returncode:return
        glyphs=json.loads(result.stdout)
        for run in runs:
            if run.get('orientation')!='horizontal':continue
            candidates=sorted((g for g in glyphs if abs(g['size']-run['font_size'])<1e-4
                and abs(g['origin'][1]-run['baseline'])<1e-4 and run['x0']-.001<=g['origin'][0]<=run['x1']+.001),key=lambda g:g['origin'][0])
            compact=lambda value:''.join(value.split())
            if compact(''.join(g['text']for g in candidates))!=compact(run['text']):continue
            painted=[g['bbox']for g in candidates if not g['text'].isspace()and g['bbox'][2]>g['bbox'][0]and g['bbox'][3]>g['bbox'][1]]
            if painted:
                # Conservative extra0.2pt around measured ink, not a relaxed
                # overlap threshold or a font-size/graph modification.
                run['ink_box']=(min(b[0]for b in painted)-.2,min(b[1]for b in painted)-.2,max(b[2]for b in painted)+.2,max(b[3]for b in painted)+.2)
    except (ValueError,OSError,subprocess.SubprocessError,KeyError,TypeError):return


def titles_visible(svg, **titles):
    root = ET.parse(svg).getroot()
    runs = text_runs(root)
    _add_positioned_ink(root,runs)
    compact = lambda s: "".join(s.split()).casefold()
    result = {}
    for key, value in titles.items():
        candidates = _title_candidates(root, runs, key)
        result[key] = not value or compact(value) in {
            compact(r["text"]) for r in candidates
        }
    return result


def title_font_size(svg, graph_title):
    root = ET.parse(svg).getroot()
    compact = lambda s: "".join(s.split()).casefold()
    sizes = {
        r["font_size"]
        for r in _title_candidates(root, text_runs(root), "graph_title")
        if compact(r["text"]) == compact(graph_title)
    }
    if len(sizes) != 1:
        raise ValueError("Native graph-title font size could not be verified")
    return sizes.pop()


if __name__ == "__main__":
    render(sys.argv[1], sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 3)
