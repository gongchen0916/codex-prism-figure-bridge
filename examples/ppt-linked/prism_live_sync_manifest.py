#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

from PIL import Image, ImageChops

import prism_ppt_sync


BASE = Path(__file__).resolve().parent
PRISM_TO_POWERPOINT_POINT_SCALE = 1.01


def ensure_white_eraser() -> Path:
    eraser = BASE / "white_eraser.tif"
    if not eraser.exists():
        Image.new("RGB", (1, 1), (255, 255, 255)).save(eraser, format="TIFF", compression="tiff_lzw", dpi=(600, 600))
    return eraser


def image_size_points(path: Path, fallback_dpi: float = 600.0) -> tuple[float, float]:
    with Image.open(path) as im:
        width_px, height_px = im.size
        dpi = im.info.get("dpi") or (fallback_dpi, fallback_dpi)
        dpi_x = float(dpi[0] or fallback_dpi)
        dpi_y = float(dpi[1] or fallback_dpi)
    return width_px / dpi_x * 72.0, height_px / dpi_y * 72.0


def normalize_tif_for_powerpoint(src: Path, dst: Path, *, trim_white: bool = True, margin_px: int = 12) -> Path:
    with Image.open(src) as im:
        dpi = im.info.get("dpi") or (600, 600)
        rgb = im.convert("RGB")
        if trim_white:
            bg = Image.new("RGB", rgb.size, (255, 255, 255))
            diff = ImageChops.difference(rgb, bg).convert("L")
            mask = diff.point(lambda p: 255 if p > 8 else 0)
            bbox = mask.getbbox()
            if bbox:
                left = max(0, bbox[0] - margin_px)
                top = max(0, bbox[1] - margin_px)
                right = min(rgb.width, bbox[2] + margin_px)
                bottom = min(rgb.height, bbox[3] + margin_px)
                rgb = rgb.crop((left, top, right, bottom))
        rgb.save(dst, format="TIFF", compression="tiff_lzw", dpi=dpi)
    return dst


def log(manifest: Path, message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    log_path = manifest.with_suffix(".live.log")
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def load_manifest(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    data["pptx"] = str(Path(data["pptx"]).expanduser().resolve())
    data["workdir"] = str(Path(data.get("workdir", path.parent / "sync_work")).expanduser().resolve())
    data["linked_dir"] = str(Path(data.get("linked_dir", path.parent / "linked_tif")).expanduser().resolve())
    for item in data["items"]:
        item["source"] = str(Path(item["source"]).expanduser().resolve())
    return data


def save_manifest(path: Path, data: dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def item_mtimes(data: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in data["items"]:
        source = Path(item["source"])
        out[item["id"]] = source.stat().st_mtime if source.exists() else 0.0
    return out


def export_item(manifest: Path, data: dict, item: dict, timeout: int, tif_resolution: int) -> Path:
    source = Path(item["source"])
    if not source.exists():
        raise FileNotFoundError(source)
    workdir = Path(data["workdir"])
    linked_dir = Path(data["linked_dir"])
    workdir.mkdir(parents=True, exist_ok=True)
    linked_dir.mkdir(parents=True, exist_ok=True)

    run_id = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
    graph_index = int(item.get("graph_index", 1))
    out_tif = workdir / f"{item['id']}_{run_id}.tif"
    script = prism_ppt_sync.write_prism_export_script(
        source,
        None,
        graph_index=graph_index,
        out_tif=out_tif,
        tif_resolution=tif_resolution,
        script_path=workdir / f"{item['id']}_{run_id}_export.pzc",
    )
    prism_ppt_sync.run_prism_script(script, timeout=timeout)
    if not out_tif.exists():
        raise FileNotFoundError(f"Prism did not create expected TIF export: {out_tif}")

    linked_target = linked_dir / str(item.get("tif_name", f"{item['id']}.tif"))
    tmp_target = linked_target.with_suffix(linked_target.suffix + ".tmp")
    normalize_tif_for_powerpoint(out_tif, tmp_target)
    tmp_target.replace(linked_target)
    width_pt, height_pt = image_size_points(linked_target)
    log(manifest, f"exported {item['id']} -> {linked_target.name} {prism_ppt_sync.sha256(linked_target)[:16]} size={width_pt:.2f}x{height_pt:.2f}pt")
    return linked_target


def update_powerpoint_overlay(manifest: Path, data: dict, item: dict, image_path: Path) -> None:
    pptx = Path(data["pptx"])
    overlay_name = f"Codex Prism live {item['id']}"
    eraser_name = f"Codex Prism eraser {item['id']}"
    shape_names = item.get("shape_names", [])
    frame = item.get("frame")
    url = "prismbridge://open?manifest=" + quote(str(manifest.resolve())) + "&id=" + quote(str(item["id"])) + "&path=" + quote(str(Path(item["source"]).resolve()))
    image_width, image_height = image_size_points(image_path)
    baseline_scale = float(item.get("scale", PRISM_TO_POWERPOINT_POINT_SCALE) or PRISM_TO_POWERPOINT_POINT_SCALE)
    eraser_path = ensure_white_eraser()
    if shape_names:
        name_conditions = " or ".join(f"nm is {applescript_string(str(name))}" for name in shape_names)
    else:
        name_conditions = "false"

    if frame:
        fallback_script = f'''
        set l to {float(frame[0])}
        set t to {float(frame[1])}
        set w to {float(frame[2])}
        set h to {float(frame[3])}
        set statusText to "used manifest frame for {item['id']}"
'''
    else:
        fallback_script = f'return "missing target shape and frame for {item["id"]}"'

    script = f'''
set targetPath to {applescript_string(str(pptx))}
tell application "Microsoft PowerPoint"
  if (count of presentations) > 0 then
    if (full name of active presentation) is targetPath then
      tell active presentation
        repeat with i from (count of shapes of slide {int(item.get("slide", 1))}) to 1 by -1
          if (name of shape i of slide {int(item.get("slide", 1))} as text) is {applescript_string(overlay_name)} then delete shape i of slide {int(item.get("slide", 1))}
          if (name of shape i of slide {int(item.get("slide", 1))} as text) is {applescript_string(eraser_name)} then delete shape i of slide {int(item.get("slide", 1))}
        end repeat
        set targetShape to missing value
        repeat with i from 1 to count of shapes of slide {int(item.get("slide", 1))}
          set nm to name of shape i of slide {int(item.get("slide", 1))} as text
          if {name_conditions} then
            set targetShape to shape i of slide {int(item.get("slide", 1))}
            exit repeat
          end if
        end repeat
        set statusText to "overlaid {item['id']}"
        if targetShape is missing value then
{fallback_script}
        else
          set l to left position of targetShape
          set t to top of targetShape
          set w to width of targetShape
          set h to height of targetShape
        end if
        set erasePic to make new picture at end of slide {int(item.get("slide", 1))} with properties {{file name:{applescript_string(str(eraser_path))}, top:t, left position:l, height:h, width:w}}
        set name of erasePic to {applescript_string(eraser_name)}
        set cX to l + (w / 2)
        set cY to t + (h / 2)
        set naturalW to {image_width}
        set naturalH to {image_height}
        set bridgeScale to {baseline_scale}
        if bridgeScale is 0 then set bridgeScale to (h / naturalH)
        set newW to naturalW * bridgeScale
        set newH to naturalH * bridgeScale
        set newL to cX - (newW / 2)
        set newT to cY - (newH / 2)
        set newPic to make new picture at end of slide {int(item.get("slide", 1))} with properties {{file name:{applescript_string(str(image_path))}, top:newT, left position:newL, height:newH, width:newW}}
        set name of newPic to {applescript_string(overlay_name)}
        set theAction to (get action setting for newPic event mouse activation mouse click)
        set action of theAction to action type hyperlink action
        set hyperlink address of hyperlink of theAction to {applescript_string(url)}
        return statusText
      end tell
    end if
  end if
  open POSIX file targetPath
  return "opened target presentation; overlay will run on next refresh"
end tell
'''
    proc = subprocess.run(["/usr/bin/osascript", "-e", script], text=True, capture_output=True, check=False, timeout=60)
    message = (proc.stdout or proc.stderr).strip()
    log(manifest, f"PowerPoint: {message}")


def powerpoint_has_presentation(pptx: Path) -> bool:
    script = f'''
set targetPath to {applescript_string(str(pptx))}
tell application "Microsoft PowerPoint"
  repeat with p in presentations
    if (full name of p) is targetPath then return "yes"
  end repeat
end tell
return "no"
'''
    proc = subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        text=True,
        capture_output=True,
        check=False,
        timeout=8,
    )
    return (proc.stdout or "").strip() == "yes"


def refresh_items(manifest: Path, data: dict, items: list[dict], timeout: int, tif_resolution: int) -> None:
    for item in items:
        image_path = export_item(manifest, data, item, timeout, tif_resolution)
        update_powerpoint_overlay(manifest, data, item, image_path)


def command_watch(args: argparse.Namespace) -> int:
    manifest = args.manifest.expanduser().resolve()
    data = load_manifest(manifest)
    last = item_mtimes(data)
    missing_count = 0
    max_missing_count = max(1, int(args.exit_after_ppt_closed / max(args.interval, 0.1)))
    log(manifest, f"watching {manifest}")
    log(manifest, f"target pptx {data['pptx']}")
    while True:
        data = load_manifest(manifest)
        if args.exit_when_ppt_closed:
            if powerpoint_has_presentation(Path(data["pptx"])):
                missing_count = 0
            else:
                missing_count += 1
                if missing_count >= max_missing_count:
                    log(manifest, f"target pptx is closed; exiting watcher: {data['pptx']}")
                    return 0
        current = item_mtimes(data)
        changed = [item for item in data["items"] if current.get(item["id"], 0.0) != last.get(item["id"], 0.0)]
        if changed:
            log(manifest, "detected save: " + ", ".join(item["id"] for item in changed))
            time.sleep(args.debounce)
            last = item_mtimes(data)
            try:
                refresh_items(manifest, data, changed, args.timeout, args.tif_resolution)
            except Exception as exc:
                log(manifest, f"refresh failed: {exc!r}")
        time.sleep(args.interval)


def command_once(args: argparse.Namespace) -> int:
    manifest = args.manifest.expanduser().resolve()
    data = load_manifest(manifest)
    refresh_items(manifest, data, data["items"], args.timeout, args.tif_resolution)
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--debounce", type=float, default=2.0)
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--tif-resolution", type=int, default=600)
    ap.add_argument("--exit-when-ppt-closed", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--exit-after-ppt-closed", type=float, default=30.0)
    ap.add_argument("--once", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.once:
        return command_once(args)
    return command_watch(args)


if __name__ == "__main__":
    raise SystemExit(main())
