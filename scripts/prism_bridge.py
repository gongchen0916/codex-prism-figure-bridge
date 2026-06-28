#!/usr/bin/env python3
"""Bridge raw data, Prism templates, Prism export scripts, and PowerPoint decks.

The bridge is intentionally file-first. Prism has no full public API, so the
stable path is to mutate Prism XML/template data and let Prism recalculate and
render the graphs.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import time
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parent
SKILL_ROOT = ROOT.parent
DEFAULT_PRISM_APP = Path("/Applications/Prism 11.app")
DEFAULT_TEMPLATE_ROOT = DEFAULT_PRISM_APP / "Contents/SharedSupport/Portfolio"
SKILL_TEMPLATE_ROOT = SKILL_ROOT / "assets/templates"
SKILL_TEMPLATE_INDEX = SKILL_TEMPLATE_ROOT / "template_index.json"
PPTX_NS = {
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}

TEMPLATE_ALIASES = {
    "column-scatter": "Graphs to explore/Column scatter.pzt",
    "box-whisker": "Graphs to explore/Box and whiskers graph.pzt",
    "box-whisker-asterisks": "Graphs to explore/Box and whiskers with asterisks.pzt",
    "before-after": "Graphs to explore/Before-after.pzt",
    "before-after-error": "Graphs with tutorials/Before-after with error.pzt",
    "volcano": "Graphs to explore/Volcano plot.pzt",
    "grouped-bars": "Graphs with tutorials/Grouped graph spacing.pzt",
    "points-grouped-bars": "Graphs with tutorials/Points and grouped bars.pzt",
    "dose-response": "Graphs with tutorials/Dose-response curves.pzt",
    "scatter-bars": "Graphs to explore/Scatter plot with bars.pzt",
    "spaghetti": "Graphs to explore/Spaghetti plot.pzt",
}


def qname(tag: str, ns: str | None) -> str:
    return f"{{{ns}}}{tag}" if ns else tag


def namespace(root: ET.Element) -> str | None:
    if root.tag.startswith("{"):
        return root.tag[1:].split("}", 1)[0]
    return None


def read_table(path: Path) -> list[list[str]]:
    text = path.read_text(encoding="utf-8-sig")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel_tab if "\t" in sample else csv.excel
    rows = [[cell.strip() for cell in row] for row in csv.reader(text.splitlines(), dialect)]
    rows = [row for row in rows if any(cell != "" for cell in row)]
    if not rows:
        raise ValueError(f"No data rows found in {path}")
    return rows


def split_header(rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    first = rows[0]
    numeric = 0
    for cell in first:
        try:
            float(cell)
            numeric += 1
        except ValueError:
            pass
    if numeric <= max(0, len(first) // 2 - 1):
        return first, rows[1:]
    return [f"Y{i + 1}" for i in range(len(first))], rows


def resolve_template(template: str) -> Path:
    candidate = Path(template).expanduser()
    if candidate.exists():
        return candidate.resolve()

    if SKILL_TEMPLATE_INDEX.exists():
        index = json.loads(SKILL_TEMPLATE_INDEX.read_text(encoding="utf-8"))
        for entry in index.get("templates", []):
            if template in {entry.get("alias"), entry.get("name")}:
                indexed = SKILL_TEMPLATE_ROOT / entry["relative_path"]
                if indexed.exists():
                    return indexed.resolve()

    portfolio = SKILL_TEMPLATE_ROOT / "portfolio"
    if template in TEMPLATE_ALIASES:
        candidate = portfolio / TEMPLATE_ALIASES[template]
        if candidate.exists():
            return candidate.resolve()
        candidate = DEFAULT_TEMPLATE_ROOT / TEMPLATE_ALIASES[template]
        if candidate.exists():
            return candidate.resolve()

    raise FileNotFoundError(f"Unknown template {template!r}. Use list-templates or prism_match_template.")


def is_zip(path: Path) -> bool:
    return zipfile.is_zipfile(path)


def first_table(root: ET.Element, ns: str | None) -> ET.Element:
    table = root.find(f".//{qname('Table', ns)}")
    if table is None:
        raise ValueError("No <Table> found in template")
    return table


def clear_children(parent: ET.Element, names: Iterable[str], ns: str | None) -> None:
    wanted = {qname(name, ns) for name in names}
    for child in list(parent):
        if child.tag in wanted:
            parent.remove(child)


def make_d(value: str, ns: str | None) -> ET.Element:
    d = ET.Element(qname("d", ns))
    d.text = value
    return d


def make_y_column(title: str, values: list[str], template: ET.Element | None, ns: str | None) -> ET.Element:
    if template is not None:
        col = deepcopy(template)
        clear_children(col, ["Title", "Subcolumn"], ns)
    else:
        col = ET.Element(qname("YColumn", ns), {"Width": "70", "Subcolumns": "1"})
    title_el = ET.Element(qname("Title", ns))
    title_el.text = title
    sub = ET.Element(qname("Subcolumn", ns))
    for value in values:
        sub.append(make_d(value, ns))
    col.append(title_el)
    col.append(sub)
    return col


def y_column_xml(title: str, values: list[str], attrs: str = 'Width="70" Decimals="0" Subcolumns="1"') -> str:
    cells = "\n".join(f"<d>{html.escape(value)}</d>" for value in values)
    return (
        f"<YColumn {attrs}>\n"
        f"<Title>{html.escape(title)}</Title>\n"
        "<Subcolumn>\n"
        f"{cells}\n"
        "</Subcolumn>\n"
        "</YColumn>"
    )


def patch_column_template_preserving(template_path: Path, data_path: Path, out: Path, title: str | None = None) -> None:
    text = template_path.read_text(encoding="utf-8", errors="replace")
    if "<XColumn" in text:
        raise ValueError("Template-preserving patcher supports column templates without XColumn only.")

    rows = read_table(data_path)
    headers, body = split_header(rows)

    if title:
        title_match = re.search(r"<Title>.*?</Title>", text, flags=re.S)
        if title_match:
            text = text[: title_match.start()] + f"<Title>{html.escape(title)}</Title>" + text[title_match.end() :]

    y_matches = list(re.finditer(r"<YColumn\b[^>]*>.*?</YColumn>", text, flags=re.S))
    if not y_matches:
        raise ValueError(f"No YColumn blocks found in template: {template_path}")
    first_attrs_match = re.match(r"<YColumn\s+([^>]*)>", y_matches[0].group(0), flags=re.S)
    attrs = first_attrs_match.group(1) if first_attrs_match else 'Width="70" Decimals="0" Subcolumns="1"'

    columns = []
    for index, header in enumerate(headers):
        values = [row[index] if len(row) > index else "" for row in body]
        columns.append(y_column_xml(header or f"Y{index + 1}", values, attrs))
    replacement = "\n".join(columns)
    text = text[: y_matches[0].start()] + replacement + text[y_matches[-1].end() :]

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")


def replace_legacy_xml_data(template: Path, data: Path, out: Path, title: str | None = None) -> None:
    rows = read_table(data)
    headers, body = split_header(rows)
    tree = ET.parse(template)
    root = tree.getroot()
    ns = namespace(root)
    if ns:
        ET.register_namespace("", ns)

    table = first_table(root, ns)
    table_type = (table.get("TableType") or "").lower()
    has_x = table.find(qname("XColumn", ns)) is not None
    xy_mode = has_x and table_type not in {"oneway", "column"}

    if xy_mode and len(headers) < 2:
        raise ValueError("XY templates need at least one X column and one Y column")

    title_el = table.find(qname("Title", ns))
    if title and title_el is not None:
        title_el.text = title

    y_template = table.find(qname("YColumn", ns))
    for y in list(table.findall(qname("YColumn", ns))):
        table.remove(y)

    if xy_mode:
        x_values = [row[0] if len(row) > 0 else "" for row in body]
        x_col = table.find(qname("XColumn", ns))
        if x_col is None:
            x_col = ET.Element(qname("XColumn", ns), {"Width": "70"})
            table.append(x_col)
        clear_children(x_col, ["d"], ns)
        for value in x_values:
            x_col.append(make_d(value, ns))
        for index, header in enumerate(headers[1:], start=1):
            values = [row[index] if len(row) > index else "" for row in body]
            table.append(make_y_column(header or f"Y{index}", values, y_template, ns))
    else:
        for index, header in enumerate(headers):
            values = [row[index] if len(row) > index else "" for row in body]
            table.append(make_y_column(header or f"Y{index + 1}", values, y_template, ns))

    out.parent.mkdir(parents=True, exist_ok=True)
    tree.write(out, encoding="UTF-8", xml_declaration=True)


def build_project(template: Path, data: Path, out: Path, title: str | None = None) -> str:
    if is_zip(template):
        raise ValueError(
            f"{template} is a Prism zip bundle. Pick a legacy XML portfolio template from prism_match_template."
        )

    sample = template.read_text(encoding="utf-8", errors="replace")[:50000]
    if "<XColumn" not in sample:
        patch_column_template_preserving(template, data, out, title)
        return "template-preserving column patch"

    replace_legacy_xml_data(template, data, out, title)
    return "element-tree xy patch"


def run_osascript(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["osascript", "-e", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def posix_to_hfs(path: Path) -> str:
    if platform.system() != "darwin":
        return str(path)
    cp = run_osascript(f"return POSIX file {json.dumps(str(path))} as text")
    if cp.returncode != 0:
        raise RuntimeError(cp.stdout.strip() or f"Unable to convert path for Prism: {path}")
    return cp.stdout.strip()


def read_prism_log(path: Path) -> str:
    if not path.exists():
        return ""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16-le", "utf-32-le"):
        try:
            text = raw.decode(encoding)
            if text.strip():
                return text.replace("\x00", "").strip()
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace").replace("\x00", "").strip()


def create_export_script(
    project: Path,
    outdir: Path,
    basename: str,
    *,
    keep_prism_warm: bool = True,
) -> Path:
    script = outdir / f"{basename}_export.pzc"
    outdir_hfs = posix_to_hfs(Path(str(outdir) + "/")) if platform.system() == "Darwin" else str(outdir)
    project_name = project.name
    lines = [
        "CreateLog",
        f'SetPath "{outdir_hfs}"',
        f'Open "{project_name}"',
        "GoTo G, 1",
        f'ExportSVG "{basename}.svg"',
        'OpenOutput "done.txt", CLEAR',
        'WText "done"',
        "CloseOutput",
    ]
    if not keep_prism_warm:
        lines.append("Close")
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return script


def run_prism_script(script: Path, timeout: int = 120) -> tuple[bool, str]:
    if platform.system() != "Darwin":
        return False, "Automatic Prism execution is currently implemented for macOS only."
    if not DEFAULT_PRISM_APP.exists():
        return False, f"Prism app not found at {DEFAULT_PRISM_APP}"

    log = script.with_suffix(".log")
    done = script.parent / "done.txt"
    svg = script.parent / script.name.replace("_export.pzc", ".svg")
    old_log_mtime = log.stat().st_mtime if log.exists() else None
    old_done_mtime = done.stat().st_mtime if done.exists() else None

    subprocess.run(["open", "-a", "Prism 11", str(script)], check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if done.exists() and (old_done_mtime is None or done.stat().st_mtime != old_done_mtime):
            return True, read_prism_log(log) or "Prism script completed."
        if svg.exists():
            return True, read_prism_log(log) or "Prism exported SVG."
        if log.exists() and (old_log_mtime is None or log.stat().st_mtime != old_log_mtime):
            text = read_prism_log(log)
            if "COMPLETE! No Errors." in text:
                return True, text
            if "PROBLEM! Not completed because of error." in text:
                return False, text
        time.sleep(0.5)

    return False, read_prism_log(log) or "Timed out waiting for Prism SVG export."


def run_prism_export_staged(
    project: Path,
    outdir: Path,
    basename: str,
    *,
    timeout: int = 120,
    keep_prism_warm: bool = True,
) -> tuple[bool, str, Path]:
    """Run Prism export from a short staging path, then copy SVG/log back.

    Prism's macOS script runner can hang on `Open` when the script directory is
    a long POSIX/HFS path. A short /private/tmp staging directory avoids that
    failure mode while preserving the public output paths.
    """
    stage_root = Path("/private/tmp/prism_bridge_runs")
    stage = stage_root / f"{basename}_{int(time.time() * 1000)}"
    stage.mkdir(parents=True, exist_ok=True)
    staged_project = stage / project.name
    shutil.copy2(project, staged_project)
    staged_script = create_export_script(staged_project, stage, basename, keep_prism_warm=keep_prism_warm)
    ok, log_text = run_prism_script(staged_script, timeout=timeout)

    staged_svg = stage / f"{basename}.svg"
    staged_log = staged_script.with_suffix(".log")
    final_svg = outdir / f"{basename}.svg"
    final_log = outdir / f"{basename}_export.log"
    if staged_svg.exists():
        shutil.copy2(staged_svg, final_svg)
    if staged_log.exists():
        shutil.copy2(staged_log, final_log)
    return ok and final_svg.exists(), log_text, final_svg


def pptx_xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def minimal_pptx(image: Path, pzfx: Path, out: Path, title: str) -> None:
    if not image.exists():
        raise FileNotFoundError(f"Image not found for PPTX: {image}")
    out.parent.mkdir(parents=True, exist_ok=True)
    image_ext = image.suffix.lower().lstrip(".")
    if image_ext not in {"png", "jpg", "jpeg", "svg"}:
        raise ValueError("PPTX image must be png, jpg, jpeg, or svg")
    content_type = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "svg": "image/svg+xml",
    }[image_ext]
    rel_path = pzfx.resolve().as_uri()
    img_name = f"image1.{image_ext}"

    slide = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="{PPTX_NS['a']}" xmlns:r="{PPTX_NS['r']}" xmlns:p="{PPTX_NS['p']}">
  <p:cSld><p:spTree>
    <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
    <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
    <p:sp>
      <p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
      <p:spPr><a:xfrm><a:off x="457200" y="228600"/><a:ext cx="8229600" cy="457200"/></a:xfrm></p:spPr>
      <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="en-US" sz="2400"/><a:t>{pptx_xml_escape(title)}</a:t></a:r></a:p></p:txBody>
    </p:sp>
    <p:pic>
      <p:nvPicPr><p:cNvPr id="3" name="Prism figure"><a:hlinkClick r:id="rId2"/></p:cNvPr><p:cNvPicPr/><p:nvPr/></p:nvPicPr>
      <p:blipFill><a:blip r:embed="rId1"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>
      <p:spPr><a:xfrm><a:off x="914400" y="914400"/><a:ext cx="7315200" cy="4114800"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
    </p:pic>
  </p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>'''
    files = {
        "[Content_Types].xml": f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{PPTX_NS['ct']}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Default Extension="{image_ext}" ContentType="{content_type}"/><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/><Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/></Types>''',
        "_rels/.rels": f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{PPTX_NS['rel']}"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/></Relationships>''',
        "ppt/presentation.xml": f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="{PPTX_NS['a']}" xmlns:r="{PPTX_NS['r']}" xmlns:p="{PPTX_NS['p']}"><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst><p:sldSz cx="9144000" cy="5143500" type="screen16x9"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>''',
        "ppt/_rels/presentation.xml.rels": f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{PPTX_NS['rel']}"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/></Relationships>''',
        "ppt/slides/slide1.xml": slide,
        "ppt/slides/_rels/slide1.xml.rels": f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{PPTX_NS['rel']}"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/{img_name}"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="{pptx_xml_escape(rel_path)}" TargetMode="External"/></Relationships>''',
    }
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text)
        z.write(image, f"ppt/media/{img_name}")


def list_templates() -> None:
    if SKILL_TEMPLATE_INDEX.exists():
        curated_path = SKILL_TEMPLATE_ROOT / "curated_templates.json"
        whitelist: set[str] = set()
        if curated_path.exists():
            whitelist = set(json.loads(curated_path.read_text(encoding="utf-8")).get("whitelist_automation", []))
        index = json.loads(SKILL_TEMPLATE_INDEX.read_text(encoding="utf-8"))
        for entry in sorted(index.get("templates", []), key=lambda item: item.get("alias", "")):
            if entry.get("alias") not in whitelist:
                continue
            path = SKILL_TEMPLATE_ROOT / entry["relative_path"]
            exists = "ok" if path.exists() else "missing"
            print(
                f"{entry['alias']:32} {exists:7} {entry.get('kind', 'unknown'):10} "
                f"{entry.get('table_type') or '-':8} {path.name}"
            )
        return

    for alias, rel in sorted(TEMPLATE_ALIASES.items()):
        path = DEFAULT_TEMPLATE_ROOT / rel
        kind = "zip-bundle" if path.exists() and is_zip(path) else "legacy-xml"
        exists = "ok" if path.exists() else "missing"
        print(f"{alias:22} {exists:7} {kind:10} {path}")


def environment() -> None:
    print(f"platform: {platform.platform()}")
    print(f"prism_app: {DEFAULT_PRISM_APP if DEFAULT_PRISM_APP.exists() else 'not found'}")
    print(f"export_format: svg")
    try:
        out = subprocess.check_output(
            ["osascript", "-e", 'tell application "Prism 11" to version'],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        ).strip()
    except Exception as exc:
        out = f"unavailable ({exc})"
    print(f"prism_version: {out}")
    ppt = Path("/Applications/Microsoft PowerPoint.app")
    print(f"powerpoint_app: {ppt if ppt.exists() else 'not found'}")
    prefs = Path.home() / "Library/Preferences/com.GraphPad.Prism.plist"
    if prefs.exists():
        with prefs.open("rb") as f:
            data = plistlib.load(f)
        print(f"prism_startup_dialog_seen: {'NSWindow Frame StartupDialog 11' in data}")
    print("automation: open .pzc via Prism (no GUI click automation)")
    print("ppt_editable_ole: unsupported on macOS; supported only by Windows OLE/ActiveX")


def build(args: argparse.Namespace) -> None:
    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    template = resolve_template(args.template)
    basename = args.name or Path(args.data).stem
    project = outdir / f"{basename}.pzfx"

    patch_mode = build_project(template, Path(args.data).expanduser(), project, args.title)
    script = create_export_script(project, outdir, basename, keep_prism_warm=not args.close_prism)

    print(f"project: {project}")
    print(f"export_script: {script}")
    print(f"patch_mode: {patch_mode}")

    exported_image = None
    if args.run_prism:
        ok, message, staged_svg = run_prism_export_staged(
            project,
            outdir,
            basename,
            timeout=args.timeout,
            keep_prism_warm=not args.close_prism,
        )
        print(f"prism_run: {'ok' if ok else 'failed'}")
        if not ok:
            print(message.strip())
        svg_path = outdir / f"{basename}.svg"
        if svg_path.exists():
            exported_image = svg_path

    if args.image:
        exported_image = Path(args.image).expanduser().resolve()
    if args.pptx:
        if exported_image is None:
            raise SystemExit(
                "PPTX requested but no SVG is available. Use --run-prism after "
                "Prism startup is cleared, or pass --image path/to/exported.svg."
            )
        pptx_path = Path(args.pptx).expanduser().resolve()
        minimal_pptx(exported_image, project, pptx_path, args.title or basename)
        print(f"pptx: {pptx_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("env", help="Check local Prism/PowerPoint bridge capabilities")
    sub.add_parser("list-templates", help="List automation-whitelisted template aliases")

    p_build = sub.add_parser("build", help="Create a Prism project and optional PPTX")
    p_build.add_argument("--data", required=True, help="CSV/TSV raw data")
    p_build.add_argument("--template", default="column-scatter", help="Template alias or path")
    p_build.add_argument("--outdir", default="outputs/prism_bridge_run", help="Output folder")
    p_build.add_argument("--name", help="Base output name")
    p_build.add_argument("--title", help="Graph/table title")
    p_build.add_argument("--run-prism", action="store_true", help="Ask Prism to export SVG")
    p_build.add_argument("--close-prism", action="store_true", help="Close Prism project after export")
    p_build.add_argument("--timeout", type=int, default=120, help="Prism script timeout seconds")
    p_build.add_argument("--image", help="Existing SVG/PNG/JPG to place in PPTX")
    p_build.add_argument("--pptx", help="Write a one-slide PPTX")

    args = parser.parse_args(argv)
    if args.cmd == "env":
        environment()
    elif args.cmd == "list-templates":
        list_templates()
    elif args.cmd == "build":
        build(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
