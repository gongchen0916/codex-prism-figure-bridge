#!/usr/bin/env python3
"""Validate bundled Prism templates by opening them in Prism and exporting a preview."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import time
from pathlib import Path


PRISM_EXTS = {".pzt", ".pzf", ".pzfx", ".prism"}


def load_bridge(skill_root: Path):
    bridge_path = skill_root / "scripts/prism_bridge.py"
    spec = importlib.util.spec_from_file_location("prism_bridge", bridge_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {bridge_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_one(bridge, template: Path, work_root: Path, index: int, timeout: int) -> dict:
    case = work_root / f"{index:04d}"
    case.mkdir(parents=True, exist_ok=True)
    local = case / f"template{template.suffix.lower()}"
    shutil.copy2(template, local)
    hfs = bridge.posix_to_hfs(Path(str(case) + "/"))
    script = case / "validate_export.pzc"
    script.write_text(
        "\n".join(
            [
                "CreateLog",
                f'SetPath "{hfs}"',
                f'Open "{local.name}"',
                "GoTo G, 1",
                'ExportSVG "preview.svg"',
                'OpenOutput "done.txt", CLEAR',
                'WText "done"',
                "CloseOutput",
                "Close",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    ok, log = bridge.run_prism_script(script, timeout)
    preview = case / "preview.svg"
    done = case / "done.txt"
    status = "ok" if ok and (preview.exists() or done.exists()) else "failed"
    return {
        "template": str(template),
        "workdir": str(case),
        "status": status,
        "preview": str(preview) if preview.exists() else None,
        "log": log,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--collection", default="downloaded")
    parser.add_argument("--timeout", type=int, default=35)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    skill_root = Path(args.skill_root).resolve()
    collection_root = skill_root / "assets/templates" / args.collection
    work_root = skill_root / "assets/templates" / f"{args.collection}_validation"
    work_root.mkdir(parents=True, exist_ok=True)

    bridge = load_bridge(skill_root)
    templates = sorted(p for p in collection_root.rglob("*") if p.is_file() and p.suffix.lower() in PRISM_EXTS)
    if args.limit:
        templates = templates[: args.limit]

    results = []
    start = time.time()
    for i, template in enumerate(templates, start=1):
        print(f"[{i}/{len(templates)}] {template.name}", flush=True)
        try:
            result = validate_one(bridge, template, work_root, i, args.timeout)
        except Exception as exc:
            result = {"template": str(template), "status": "error", "preview": None, "log": str(exc)}
        results.append(result)
        print(f"  -> {result['status']}", flush=True)

    summary = {
        "collection": args.collection,
        "total": len(results),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "failed": sum(1 for r in results if r["status"] != "ok"),
        "elapsed_sec": round(time.time() - start, 1),
        "results": results,
    }
    out = skill_root / "assets/templates" / f"{args.collection}_validation.json"
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("total", "ok", "failed", "elapsed_sec")}, ensure_ascii=False))
    return 0 if summary["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
