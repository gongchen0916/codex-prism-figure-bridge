#!/usr/bin/env python3
"""Import downloaded Prism templates into this skill with hash de-duplication."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path


PRISM_EXTS = {".pzt", ".pzf", ".pzfx", ".prism"}


def slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return value.strip("-._") or "template"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def infer_kind(path: Path) -> str:
    if zipfile.is_zipfile(path):
        return "zip-bundle"
    raw = path.read_bytes()[:256]
    if raw.lstrip().startswith(b"<?xml") or raw.lstrip().startswith(b"<"):
        return "xml"
    return "binary"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", help="Folders to scan")
    parser.add_argument("--skill-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--collection", default="downloaded")
    args = parser.parse_args()

    skill_root = Path(args.skill_root).resolve()
    out_root = skill_root / "assets/templates" / args.collection
    out_root.mkdir(parents=True, exist_ok=True)

    files: list[Path] = []
    for source in args.sources:
        root = Path(source).expanduser().resolve()
        files.extend(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in PRISM_EXTS)

    by_hash: dict[str, list[Path]] = {}
    for path in sorted(files):
        by_hash.setdefault(sha256(path), []).append(path)

    imported = []
    for index, (digest, paths) in enumerate(sorted(by_hash.items()), start=1):
        chosen = sorted(paths, key=lambda p: (len(str(p)), str(p)))[0]
        name_slug = slug(chosen.stem)
        dest = out_root / f"{index:04d}-{name_slug}{chosen.suffix.lower()}"
        counter = 2
        while dest.exists() and sha256(dest) != digest:
            dest = out_root / f"{index:04d}-{name_slug}-{counter}{chosen.suffix.lower()}"
            counter += 1
        shutil.copy2(chosen, dest)
        imported.append(
            {
                "alias": dest.stem,
                "name": chosen.stem,
                "collection": args.collection,
                "relative_path": dest.relative_to(skill_root / "assets/templates").as_posix(),
                "kind": infer_kind(dest),
                "sha256": digest,
                "source_count": len(paths),
                "source_paths": [str(p) for p in paths],
                "validation": "not-run",
            }
        )

    manifest = {
        "collection": args.collection,
        "sources": [str(Path(s).expanduser().resolve()) for s in args.sources],
        "total_candidates": len(files),
        "unique_count": len(imported),
        "duplicate_groups": sum(1 for paths in by_hash.values() if len(paths) > 1),
        "templates": imported,
    }
    manifest_path = skill_root / "assets/templates" / f"{args.collection}_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("total_candidates", "unique_count", "duplicate_groups")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
