#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PZC = ROOT / "export_linked_svg_from_prism.pzc"
LINKED = ROOT / "linked_prism_export.pdf"
CANONICAL_EXPORTS = {
    ".pdf": ROOT / "linked_prism_export.pdf",
    ".svg": ROOT / "linked_prism_export.svg",
    ".eps": ROOT / "linked_prism_export.eps",
}
AI = ROOT / "linked_prism_figure.ai"
REFRESH_JSX = ROOT / "refresh_linked_items.jsx"
DONE = ROOT / "done.txt"
LOG = ROOT / "export_linked_svg_from_prism.log"


def latest_export_for_suffix(suffix: str) -> Path | None:
    candidates = list(ROOT.glob(f"linked_prism_export*{suffix}"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def normalize_prism_exports() -> None:
    for suffix, canonical in CANONICAL_EXPORTS.items():
        latest = latest_export_for_suffix(suffix)
        if latest is None:
            raise FileNotFoundError(f"Missing Prism export for {suffix}")
        if latest.resolve() != canonical.resolve():
            shutil.copy2(latest, canonical)
        # Remove numbered duplicates after copying back to the stable link target.
        for extra in ROOT.glob(f"linked_prism_export *{suffix}"):
            extra.unlink(missing_ok=True)


def run_prism_export(timeout: int = 90) -> None:
    old_mtime = LINKED.stat().st_mtime if LINKED.exists() else 0
    if DONE.exists():
        DONE.unlink()
    subprocess.run(["/usr/bin/open", str(PZC)], check=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if DONE.exists():
            normalize_prism_exports()
            if LINKED.exists() and LINKED.stat().st_mtime >= old_mtime:
                return
        if LINKED.exists() and LINKED.stat().st_mtime > old_mtime:
            normalize_prism_exports()
            return
        time.sleep(0.5)
    log_tail = LOG.read_text(errors="replace")[-2000:] if LOG.exists() else ""
    raise TimeoutError(f"Prism export did not refresh {LINKED}\n{log_tail}")


def refresh_illustrator_link() -> str:
    script = f'tell application "Adobe Illustrator" to do javascript file "{REFRESH_JSX}"'
    proc = subprocess.run(["osascript", "-e", script], check=True, text=True, capture_output=True)
    return proc.stdout.strip()


def main() -> None:
    run_prism_export()
    js_out = refresh_illustrator_link()
    status = {
        "ai": str(AI),
        "linked_file": str(LINKED),
        "linked_mtime": LINKED.stat().st_mtime,
        "illustrator": js_out,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
