#!/usr/bin/env python3
"""Synchronize a Prism-rendered figure image into a PowerPoint media slot.

This is a macOS replacement for the part of OLE people actually need day to
day: edit in Prism, render with Prism, update the figure that PowerPoint shows.
It does not claim to make a native editable OLE object on Mac.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image


PRISM_APP_NAME = "Prism 11"
DEFAULT_GRAPH_INDEX = 1


def clean_prism_log(data: bytes) -> str:
    """Prism .pzc logs are often UTF-16-ish with NUL bytes between chars."""
    if b"\x00" in data[:200]:
        text = data.replace(b"\x00", b"").decode("utf-8", errors="replace")
    else:
        text = data.decode("utf-8", errors="replace")
    return text.strip()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_prism_export_script(
    source: Path,
    out_png: Path | None,
    *,
    graph_index: int = DEFAULT_GRAPH_INDEX,
    out_pdf: Path | None = None,
    out_tif: Path | None = None,
    tif_resolution: int = 600,
    script_path: Path | None = None,
) -> Path:
    source = source.resolve()
    out_png = out_png.resolve() if out_png else None
    out_pdf = out_pdf.resolve() if out_pdf else None
    out_tif = out_tif.resolve() if out_tif else None
    script_path = script_path or (out_tif or out_png or source).with_suffix(".pzc")
    done = script_path.with_name(script_path.stem + "_done.txt")

    lines = [
        f'SetPath "{source.parent}"',
        f'Open "{source.name}"',
        f"GoTo G, {graph_index}",
    ]
    if out_png:
        lines.append(f'ExportPNG "{out_png}"')
    if out_tif:
        lines.append(f'ExportTIF "{out_tif}", {tif_resolution}')
    if out_pdf:
        lines.append(f'ExportPDF "{out_pdf}"')
    lines += [
        f'OpenOutput "{done.resolve()}", CLEAR',
        'WText "done"',
        "CloseOutput",
        "Close",
    ]
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return script_path


def run_prism_script(script_path: Path, *, timeout: int = 120) -> str:
    script_path = script_path.resolve()
    log_path = script_path.with_suffix(".log")
    before_mtime = log_path.stat().st_mtime if log_path.exists() else None

    subprocess.run(["/usr/bin/open", "-a", PRISM_APP_NAME, str(script_path)], check=False)
    deadline = time.time() + timeout
    last_text = ""
    while time.time() < deadline:
        if log_path.exists() and (before_mtime is None or log_path.stat().st_mtime != before_mtime):
            last_text = clean_prism_log(log_path.read_bytes())
            if "COMPLETE! No Errors." in last_text:
                return last_text
            if "PROBLEM! Not completed because of error." in last_text:
                raise RuntimeError(f"Prism script failed: {last_text}")
        time.sleep(0.75)
    raise TimeoutError(f"Timed out waiting for Prism log: {log_path}\n{last_text}")


def export_prism_staged(
    source: Path,
    *,
    workdir: Path,
    basename: str,
    graph_index: int = DEFAULT_GRAPH_INDEX,
    timeout: int = 120,
    out_png: Path | None = None,
    out_tif: Path | None = None,
    out_pdf: Path | None = None,
    tif_resolution: int = 600,
) -> dict[str, object]:
    """Export a Prism graph from a short /private/tmp staging path.

    Prism 11 on macOS can hang or fail when a .pzc script opens a source from a
    long path with spaces/non-ASCII characters. Staging both the source and the
    Prism script into /private/tmp makes the same project export reliably, then
    copies the rendered files and log back to the requested work directory.
    """
    source = source.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    stage = Path("/private/tmp/prism_ppt_sync_runs") / f"{basename}_{uuid.uuid4().hex[:10]}"
    stage.mkdir(parents=True, exist_ok=True)
    staged_source = stage / source.name
    shutil.copy2(source, staged_source)

    staged_png = stage / out_png.name if out_png else None
    staged_tif = stage / out_tif.name if out_tif else None
    staged_pdf = stage / out_pdf.name if out_pdf else None
    staged_script = stage / f"{basename}_export.pzc"
    script = write_prism_export_script(
        staged_source,
        staged_png,
        graph_index=graph_index,
        out_pdf=staged_pdf,
        out_tif=staged_tif,
        tif_resolution=tif_resolution,
        script_path=staged_script,
    )
    log_text = run_prism_script(script, timeout=timeout)

    copied: dict[str, str] = {}
    for label, staged, final in (
        ("png", staged_png, out_png),
        ("tif", staged_tif, out_tif),
        ("pdf", staged_pdf, out_pdf),
    ):
        if staged and final:
            if not staged.exists():
                raise FileNotFoundError(f"Prism did not create expected {label.upper()} export: {staged}")
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staged, final)
            copied[label] = str(final.resolve())

    final_script = workdir / f"{basename}_export.pzc"
    shutil.copy2(staged_script, final_script)
    staged_log = staged_script.with_suffix(".log")
    if staged_log.exists():
        shutil.copy2(staged_log, final_script.with_suffix(".log"))

    return {
        "stage": str(stage),
        "script": str(final_script.resolve()),
        "log": log_text,
        **copied,
    }


def list_pptx_media(pptx: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with zipfile.ZipFile(pptx, "r") as z:
        for name in sorted(z.namelist()):
            if name.startswith("ppt/media/"):
                data = z.read(name)
                rows.append({"member": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return rows


def update_relationship_xml(xml: bytes, rel_id: str, target: str) -> bytes:
    ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    ET.register_namespace("", ns)
    root = ET.fromstring(xml)
    found = False
    for rel in root:
        if rel.attrib.get("Id") == rel_id:
            rel.set("Target", target)
            found = True
    if not found:
        raise ValueError(f"Relationship id not found: {rel_id}")
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def pptx_frame_aspect(pptx: Path, rels_member: str | None, rel_id: str | None) -> float | None:
    if not rels_member or not rel_id:
        return None
    slide_member = rels_member.replace("ppt/slides/_rels/", "ppt/slides/").replace(".xml.rels", ".xml")
    ns = {
        "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    with zipfile.ZipFile(pptx, "r") as z:
        if slide_member not in z.namelist():
            return None
        root = ET.fromstring(z.read(slide_member))
    rid_attr = f"{{{ns['r']}}}embed"
    for pic in root.findall(".//p:pic", ns):
        blip = pic.find(".//a:blip", ns)
        link_attr = f"{{{ns['r']}}}link"
        if blip is None or (blip.get(rid_attr) != rel_id and blip.get(link_attr) != rel_id):
            continue
        ext = pic.find(".//a:xfrm/a:ext", ns)
        if ext is None:
            continue
        try:
            cx = int(ext.get("cx", "0"))
            cy = int(ext.get("cy", "0"))
        except ValueError:
            return None
        if cx > 0 and cy > 0:
            return cx / cy
    return None


def pad_image_to_aspect(src: Path, dst: Path, target_aspect: float, *, background=(255, 255, 255, 255)) -> Path:
    with Image.open(src) as im:
        dpi = im.info.get("dpi")
        original = im.convert("RGBA")
    w, h = original.size
    if w <= 0 or h <= 0 or target_aspect <= 0:
        shutil.copy2(src, dst)
        return dst
    current = w / h
    if abs(current - target_aspect) < 0.003:
        shutil.copy2(src, dst)
        return dst
    if current > target_aspect:
        new_w = w
        new_h = max(h, round(w / target_aspect))
    else:
        new_h = h
        new_w = max(w, round(h * target_aspect))
    canvas = Image.new("RGBA", (new_w, new_h), background)
    canvas.alpha_composite(original, ((new_w - w) // 2, (new_h - h) // 2))
    dst.parent.mkdir(parents=True, exist_ok=True)
    save_kwargs = {}
    if dpi:
        save_kwargs["dpi"] = dpi
    if dst.suffix.lower() in {".tif", ".tiff"}:
        save_kwargs["compression"] = "tiff_lzw"
    canvas.convert("RGB").save(dst, **save_kwargs)
    return dst


def pad_png_to_aspect(src: Path, dst: Path, target_aspect: float, *, background=(255, 255, 255, 255)) -> Path:
    return pad_image_to_aspect(src, dst, target_aspect, background=background)


def update_content_types_xml(xml: bytes, extension: str) -> bytes:
    content_type = {
        "png": "image/png",
        "tif": "image/tiff",
        "tiff": "image/tiff",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
    }.get(extension.lower())
    if not content_type:
        return xml
    ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    ET.register_namespace("", ns)
    root = ET.fromstring(xml)
    for default in root.findall(f"{{{ns}}}Default"):
        if default.get("Extension", "").lower() == extension.lower():
            default.set("ContentType", content_type)
            return ET.tostring(root, encoding="UTF-8", xml_declaration=True)
    default = ET.Element(f"{{{ns}}}Default")
    default.set("Extension", extension.lower())
    default.set("ContentType", content_type)
    root.insert(0, default)
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def replace_pptx_member(
    pptx: Path,
    member: str,
    replacement: Path,
    *,
    backup: bool = True,
    rels_member: str | None = None,
    rel_id: str | None = None,
) -> None:
    pptx = pptx.resolve()
    replacement = replacement.resolve()
    if not pptx.exists():
        raise FileNotFoundError(pptx)
    if not replacement.exists():
        raise FileNotFoundError(replacement)

    tmp = pptx.with_suffix(pptx.suffix + ".tmp")
    bak = pptx.with_suffix(pptx.suffix + ".bak")
    if backup and not bak.exists():
        shutil.copy2(pptx, bak)

    rel_target = "../media/" + Path(member).name
    stale_png_member = str(Path(member).with_suffix(".png")) if Path(member).suffix.lower() in {".tif", ".tiff"} else None
    with zipfile.ZipFile(pptx, "r") as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        found = False
        rels_found = False
        for info in zin.infolist():
            if stale_png_member and info.filename == stale_png_member:
                continue
            payload = zin.read(info.filename)
            if info.filename == "[Content_Types].xml":
                payload = update_content_types_xml(payload, Path(member).suffix.lstrip("."))
            if info.filename == member:
                payload = replacement.read_bytes()
                found = True
            if rels_member and rel_id and info.filename == rels_member:
                payload = update_relationship_xml(payload, rel_id, rel_target)
                rels_found = True
            zout.writestr(info, payload)
        if not found:
            zout.writestr(member, replacement.read_bytes())
        if rels_member and rel_id and not rels_found:
            tmp.unlink(missing_ok=True)
            raise ValueError(f"PPTX relationship file not found: {rels_member}")
    tmp.replace(pptx)


def refresh(
    pptx: Path,
    source: Path,
    member: str,
    *,
    workdir: Path,
    graph_index: int = DEFAULT_GRAPH_INDEX,
    timeout: int = 120,
    open_powerpoint: bool = False,
    rels_member: str | None = None,
    rel_id: str | None = None,
    ppt_format: str = "tif",
    tif_resolution: int = 600,
) -> dict[str, object]:
    workdir.mkdir(parents=True, exist_ok=True)
    # Prism silently avoids overwriting existing export files by creating
    # "name 1.png", "name 2.png", etc. Use a unique basename so the file we
    # package into PowerPoint is definitely the one Prism just rendered.
    run_id = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
    out_png = None if ppt_format == "tif" else workdir / f"{source.stem}_graph{graph_index}_{run_id}.png"
    out_tif = workdir / f"{source.stem}_graph{graph_index}_{run_id}.tif"
    export_result = export_prism_staged(
        source,
        workdir=workdir,
        basename=f"{source.stem}_graph{graph_index}_{run_id}",
        graph_index=graph_index,
        timeout=timeout,
        out_png=out_png,
        out_tif=out_tif,
        tif_resolution=tif_resolution,
    )
    log_text = str(export_result["log"])
    if out_png is not None and not out_png.exists():
        raise FileNotFoundError(f"Prism did not create expected export: {out_png}")
    if not out_tif.exists():
        raise FileNotFoundError(f"Prism did not create expected TIF export: {out_tif}")
    package_source = out_tif if ppt_format == "tif" else out_png
    if package_source is None:
        raise ValueError("No package image was exported")
    package_image = package_source
    package_member = member
    if ppt_format == "tif":
        package_member = str(Path(member).with_suffix(".tif"))
    target_aspect = pptx_frame_aspect(pptx, rels_member, rel_id)
    if target_aspect:
        package_image = package_source.with_name(package_source.stem + "_pptfit" + package_source.suffix)
        pad_image_to_aspect(package_source, package_image, target_aspect)
    replace_pptx_member(pptx, package_member, package_image, rels_member=rels_member, rel_id=rel_id)
    if open_powerpoint:
        subprocess.Popen(["/usr/bin/open", "-a", "Microsoft PowerPoint", str(pptx.resolve())])
    return {
        "pptx": str(pptx.resolve()),
        "source": str(source.resolve()),
        "member": package_member,
        "tif": str(out_tif.resolve()),
        "ppt_image": str(package_image.resolve()),
        "tif_sha256": sha256(out_tif),
        "ppt_image_sha256": sha256(package_image),
        "ppt_format": ppt_format,
        "target_aspect": target_aspect,
        "log": log_text,
        "stage": export_result["stage"],
        "script": export_result["script"],
        "rels_member": rels_member,
        "rel_id": rel_id,
    }


def load_manifest(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_manifest(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def command_inspect(args: argparse.Namespace) -> int:
    for row in list_pptx_media(args.pptx):
        print(f"{row['member']}\t{row['bytes']}\t{str(row['sha256'])[:16]}")
    return 0


def command_export(args: argparse.Namespace) -> int:
    out_tif = args.out_tif
    result = export_prism_staged(
        args.source,
        workdir=args.workdir or (args.script.parent if args.script else args.out_png.parent),
        basename=args.script.stem if args.script else f"{args.source.stem}_graph{args.graph_index}",
        graph_index=args.graph_index,
        timeout=args.timeout,
        out_png=args.out_png,
        out_tif=out_tif,
        out_pdf=args.out_pdf,
    )
    print(f"script\t{result['script']}")
    print(result["log"])
    print(f"png\t{args.out_png.resolve()}")
    if out_tif:
        print(f"tif\t{out_tif.resolve()}")
    if args.out_pdf:
        print(f"pdf\t{args.out_pdf.resolve()}")
    return 0


def command_refresh(args: argparse.Namespace) -> int:
    result = refresh(
        args.pptx,
        args.source,
        args.member,
        workdir=args.workdir,
        graph_index=args.graph_index,
        timeout=args.timeout,
        open_powerpoint=args.open_powerpoint,
        rels_member=args.rels_member,
        rel_id=args.rel_id,
        ppt_format=args.ppt_format,
        tif_resolution=args.tif_resolution,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def command_init(args: argparse.Namespace) -> int:
    data = {
        "pptx": str(args.pptx.resolve()),
        "source": str(args.source.resolve()),
        "member": args.member,
        "workdir": str(args.workdir.resolve()),
        "graph_index": args.graph_index,
        "timeout": args.timeout,
        "rels_member": args.rels_member,
        "rel_id": args.rel_id,
        "ppt_format": args.ppt_format,
        "tif_resolution": args.tif_resolution,
    }
    write_manifest(args.manifest, data)
    print(args.manifest)
    return 0


def command_run_manifest(args: argparse.Namespace) -> int:
    data = load_manifest(args.manifest)
    result = refresh(
        Path(str(data["pptx"])),
        Path(str(data["source"])),
        str(data["member"]),
        workdir=Path(str(data["workdir"])),
        graph_index=int(data.get("graph_index", DEFAULT_GRAPH_INDEX)),
        timeout=int(data.get("timeout", args.timeout)),
        open_powerpoint=args.open_powerpoint,
        rels_member=str(data["rels_member"]) if data.get("rels_member") else None,
        rel_id=str(data["rel_id"]) if data.get("rel_id") else None,
        ppt_format=str(data.get("ppt_format", "tif")),
        tif_resolution=int(data.get("tif_resolution", 600)),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def command_watch(args: argparse.Namespace) -> int:
    data = load_manifest(args.manifest)
    source = Path(str(data["source"]))
    last = source.stat().st_mtime if source.exists() else 0.0
    print(f"watching {source}; press Ctrl-C to stop", flush=True)
    while True:
        current = source.stat().st_mtime if source.exists() else 0.0
        if current != last:
            last = current
            try:
                result = refresh(
                    Path(str(data["pptx"])),
                    source,
                    str(data["member"]),
                    workdir=Path(str(data["workdir"])),
                    graph_index=int(data.get("graph_index", DEFAULT_GRAPH_INDEX)),
                    timeout=int(data.get("timeout", args.timeout)),
                    open_powerpoint=args.open_powerpoint,
                    rels_member=str(data["rels_member"]) if data.get("rels_member") else None,
                    rel_id=str(data["rel_id"]) if data.get("rel_id") else None,
                    ppt_format=str(data.get("ppt_format", "tif")),
                    tif_resolution=int(data.get("tif_resolution", 600)),
                )
                print(json.dumps(result, ensure_ascii=False), flush=True)
            except Exception as exc:
                print(f"refresh failed: {exc}", file=sys.stderr, flush=True)
        time.sleep(args.interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("inspect", help="List ppt/media members in a PPTX")
    p.add_argument("--pptx", type=Path, required=True)
    p.set_defaults(func=command_inspect)

    p = sub.add_parser("export", help="Ask Prism to export a graph from a .pzfx/.pzf")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--out-png", type=Path, required=True)
    p.add_argument("--out-tif", type=Path)
    p.add_argument("--out-pdf", type=Path)
    p.add_argument("--script", type=Path)
    p.add_argument("--workdir", type=Path)
    p.add_argument("--graph-index", type=int, default=DEFAULT_GRAPH_INDEX)
    p.add_argument("--timeout", type=int, default=120)
    p.set_defaults(func=command_export)

    p = sub.add_parser("refresh", help="Export from Prism, then replace one PPTX media image")
    p.add_argument("--pptx", type=Path, required=True)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--member", required=True, help="Example: ppt/media/image1.png")
    p.add_argument("--rels-member", help="Example: ppt/slides/_rels/slide1.xml.rels")
    p.add_argument("--rel-id", help="Image relationship id to repoint, e.g. rId19")
    p.add_argument("--workdir", type=Path, required=True)
    p.add_argument("--graph-index", type=int, default=DEFAULT_GRAPH_INDEX)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--open-powerpoint", action="store_true")
    p.add_argument("--ppt-format", choices=["tif", "png"], default="tif")
    p.add_argument("--tif-resolution", type=int, default=600)
    p.set_defaults(func=command_refresh)

    p = sub.add_parser("init", help="Write a reusable sync manifest")
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--pptx", type=Path, required=True)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--member", required=True)
    p.add_argument("--rels-member")
    p.add_argument("--rel-id")
    p.add_argument("--workdir", type=Path, required=True)
    p.add_argument("--graph-index", type=int, default=DEFAULT_GRAPH_INDEX)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--ppt-format", choices=["tif", "png"], default="tif")
    p.add_argument("--tif-resolution", type=int, default=600)
    p.set_defaults(func=command_init)

    p = sub.add_parser("run", help="Refresh using a manifest")
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--open-powerpoint", action="store_true")
    p.set_defaults(func=command_run_manifest)

    p = sub.add_parser("watch", help="Watch source .pzfx mtime and refresh when it changes")
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--interval", type=float, default=3.0)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--open-powerpoint", action="store_true")
    p.set_defaults(func=command_watch)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
