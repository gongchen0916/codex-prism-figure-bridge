#!/usr/bin/env python3
"""Rebuild the combined template index for bundled Prism template assets."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path


PRISM_EXTS = {".pzt", ".pzf", ".pzfx", ".prism"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "-", value.lower()).strip("-")
    return value or "template"


def classify(path: Path) -> dict:
    kind = "zip-bundle" if zipfile.is_zipfile(path) else "binary"
    info = {"kind": kind, "table_type": None, "has_x_column": None, "y_column_count": None, "first_title": path.stem}
    if kind != "zip-bundle":
        try:
            text = path.read_text(errors="ignore")
            if text.lstrip().startswith("<") or "<?xml" in text[:128]:
                info["kind"] = "xml"
                sample = text[:50000]
                table_type = re.search(r'<Table\b[^>]*TableType="([^"]+)"', sample)
                title = re.search(r"<Title>(.*?)</Title>", sample, flags=re.S)
                info.update(
                    {
                        "table_type": table_type.group(1) if table_type else None,
                        "has_x_column": "<XColumn" in sample,
                        "y_column_count": len(re.findall(r"<YColumn\b", sample)),
                        "first_title": re.sub(r"\s+", " ", title.group(1)).strip() if title else path.stem,
                    }
                )
        except UnicodeDecodeError:
            pass
    return info


def main() -> int:
    skill_root = Path(__file__).resolve().parents[1]
    templates_root = skill_root / "assets/templates"
    entries = []
    seen_aliases: dict[str, int] = {}
    for path in sorted(templates_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in PRISM_EXTS:
            continue
        rel = path.relative_to(templates_root).as_posix()
        base_alias = slug(path.stem)
        seen_aliases[base_alias] = seen_aliases.get(base_alias, 0) + 1
        alias = base_alias if seen_aliases[base_alias] == 1 else f"{base_alias}-{seen_aliases[base_alias]}"
        entry = {
            "alias": alias,
            "name": path.stem,
            "collection": rel.split("/", 1)[0],
            "relative_path": rel,
            "sha256": sha256(path),
        }
        entry.update(classify(path))
        entries.append(entry)
    out = {
        "source": "Bundled GraphPad Prism templates plus user-imported de-duplicated templates",
        "template_root": "assets/templates",
        "count": len(entries),
        "templates": entries,
    }
    (templates_root / "template_index.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"count": len(entries)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
