"""User template gallery and hash-keyed native reference thumbnails."""

from pathlib import Path
import argparse
import hashlib
import html
import json
import os
import re
import shutil
import tempfile
import prism_bridge as bridge
import svg_render
import template_matcher as matcher

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "assets/templates"


def preview_path(entry, graph_index=1):
    if graph_index == 1 and entry.get("preview_path"):
        p = TEMPLATES / entry["preview_path"]
        verified_hash = entry.get("verified_template_sha256")
        current_hash = hashlib.sha256(
            (TEMPLATES / entry["relative_path"]).read_bytes()
        ).hexdigest()
        if p.exists() and verified_hash == current_hash:
            return p
    key = hashlib.sha256((TEMPLATES / entry["relative_path"]).read_bytes()).hexdigest()[
        :20
    ]
    return TEMPLATES / "previews" / f"{key}_g{graph_index}.png"


def thumbnail(alias, graph_index=1, refresh=False, timeout=25):
    template = bridge.resolve_template(alias)
    entry = next(
        e
        for e in matcher.load_index(ROOT)["templates"]
        if TEMPLATES / e["relative_path"] == template
    )
    png = preview_path(entry, graph_index)
    result = {
        "alias": entry["alias"],
        "name": entry.get("source_name") or entry["name"],
        "png": str(png),
        "cached": png.exists() and not refresh,
        "reference_preview": True,
        "warning": "Template demonstration only; its original labels and statistics are not user results.",
    }
    if result["cached"]:
        return result
    stage = Path(tempfile.mkdtemp(prefix="prism_thumbnail_", dir="/private/tmp"))
    source = stage / ("template" + template.suffix)
    shutil.copy2(template, source)
    script = stage / "preview_export.pzc"
    lines = [
        "CreateLog",
        f'SetPath "{bridge.posix_to_hfs(stage)}"',
        f'Open "{source.name}"',
        f"GoTo G, {graph_index}",
        'ExportSVG "preview.svg"',
        "Close",
        'OpenOutput "done.txt", CLEAR',
        'WText "done"',
        "CloseOutput",
    ]
    script.write_text("\n".join(lines) + "\n")
    with bridge.prism_export_lock(wait_timeout=30):
        ok, log = bridge.run_prism_script(script, timeout)
    if not ok or not (stage / "preview.svg").is_file():
        png.parent.mkdir(parents=True, exist_ok=True)
        png.with_suffix(".error.json").write_text(
            json.dumps(
                {"alias": entry["alias"], "stage": str(stage), "log": log},
                ensure_ascii=False,
                indent=2,
            )
        )
        raise RuntimeError(
            f"Native template preview failed; diagnostic stage {stage}: {log}"
        )
    png.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(stage / "preview.svg", png.with_suffix(".svg"))
    svg_render.render(png.with_suffix(".svg"), png)
    result["native_svg"] = str(png.with_suffix(".svg"))
    shutil.rmtree(stage)
    return result


def catalog_entries():
    es = matcher.load_index(ROOT)["templates"]
    safe = set(matcher.load_curated(ROOT)["whitelist_automation"])
    selected = {}
    for e in sorted(
        es,
        key=lambda e: (
            e["alias"] not in safe,
            e["kind"] != "xml",
            e["collection"] != "converted",
            e["alias"],
        ),
    ):
        name = e.get("source_name") or e["name"]
        key = re.sub(r"^\d{4}_[a-f0-9]{10}_", "", name)
        if e["kind"] == "binary":
            continue
        if key not in selected:
            selected[key] = e
    return list(selected.values()), safe


def reference_preview_supported(entry):
    # A read-only native reference render does not need a patchable data table.
    # Publication/data-replacement eligibility remains a separate whitelist.
    return entry.get("kind") in {"xml", "zip-bundle"} and entry.get("graph_count") != 0


def write_gallery(path=None):
    path = Path(path or TEMPLATES / "gallery.html")
    es, safe = catalog_entries()
    cards = []
    for e in es:
        name = e.get("source_name") or e["name"]
        png = preview_path(e)
        img = (
            f'<img loading="lazy" src="{html.escape(os.path.relpath(png,path.parent))}" alt="{html.escape(name)}">'
            if png.exists()
            else '<div class="pending">可通过模板预览工具生成</div>'
        )
        if not png.exists() and png.with_suffix(".error.json").exists():
            img = '<div class="pending">原模板预览失败，保留了诊断记录</div>'
        status = "已适配" if e["alias"] in safe else "实验 / 参考"
        cards.append(
            f'<article data-search="{html.escape((name+" "+e["alias"]).lower(),quote=True)}">{img}<h2>{html.escape(name)}</h2><p>{status} · {html.escape(str(e.get("table_type") or e["kind"]))}</p><code>{html.escape(e["alias"])}</code></article>'
        )
    page = (
        """<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Prism 模板库</title>
<style>body{font:15px -apple-system,sans-serif;background:#f5f5f2;color:#242424;margin:32px}h1{font-size:28px}input{width:min(650px,90%);font:inherit;padding:12px;border:1px solid #bbb}main{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:18px;margin-top:24px}article{background:white;padding:20px;border:1px solid #ddd}img,.pending{width:100%;height:240px;object-fit:contain}h2{font-size:16px;font-weight:600}p{color:#666}code{font-size:11px;overflow-wrap:anywhere}.pending{display:grid;place-items:center;background:#f8f8f8;color:#888}[hidden]{display:none}</style>
<h1>Prism 模板库</h1><p>按中文名称、图型或编号查找。图中为模板示例数据；原模板的 P 值和标签不代表你的实验结果。</p><input id="search" placeholder="搜索：水彩、小提琴、曲线、84…"><main>"""
        + "".join(cards)
        + """</main><script>document.querySelector('#search').addEventListener('input',e=>{const q=e.target.value.toLowerCase().trim();const terms=q.match(/水彩|箱线|小提琴|曲线|散点|柱状|配对|热图|\\d+|[a-z]+/g)||[q];document.querySelectorAll('article').forEach(a=>a.hidden=!terms.every(t=>a.dataset.search.includes(t)))})</script></html>"""
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page)
    return path


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--refresh-previews", action="store_true")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--query", default="")
    args = p.parse_args()
    if args.refresh_previews:
        entries, _ = catalog_entries()
        done = 0
        for entry in entries:
            if not reference_preview_supported(entry) or (
                args.query and not matcher.matches_query(entry, args.query)
            ):
                continue
            if preview_path(entry).with_suffix(".error.json").exists():
                continue
            if preview_path(entry).exists():
                continue
            try:
                thumbnail(entry["alias"])
                print(entry["alias"], "ready", flush=True)
            except Exception as exc:
                print(str(exc), flush=True)
                break
            done += 1
            if done >= args.limit:
                break
    print(write_gallery())
