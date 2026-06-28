#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PZFX = (
    ROOT.parents[1]
    / "outputs/pain3hz_recreate_right/3Hz_right_0081_refined_prism_objectfix.pzfx"
)
REFRESH = ROOT / "refresh_prism_to_ai.py"
LOG = ROOT / "watch_prism_ai_link.log"


def log(message: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}\n"
    LOG.open("a").write(line)
    print(line, end="", flush=True)


def main() -> None:
    if not PZFX.exists():
        raise SystemExit(f"Missing Prism source: {PZFX}")
    last = PZFX.stat().st_mtime
    log(f"watching {PZFX}")
    while True:
        time.sleep(2)
        current = PZFX.stat().st_mtime
        if current == last:
            continue
        last = current
        # Debounce Prism save/write bursts.
        time.sleep(2)
        log("Prism source changed; refreshing Illustrator link")
        try:
            proc = subprocess.run(
                ["python3", str(REFRESH)],
                cwd=str(ROOT.parents[1]),
                text=True,
                capture_output=True,
                check=True,
            )
            log(proc.stdout.strip().replace("\n", " "))
        except subprocess.CalledProcessError as exc:
            log(f"refresh failed rc={exc.returncode} stdout={exc.stdout!r} stderr={exc.stderr!r}")


if __name__ == "__main__":
    main()
