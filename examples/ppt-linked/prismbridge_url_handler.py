#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


BASE = Path(__file__).resolve().parent
LIVE_SYNC = BASE / "prism_live_sync_all.py"
MANIFEST_SYNC = BASE / "prism_live_sync_manifest.py"
LOG = BASE / "prismbridge_url_handler.log"


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {message}\n")


def is_live_sync_running(script: Path, manifest: Path | None = None) -> bool:
    needle = str(script)
    proc = subprocess.run(
        ["/usr/bin/pgrep", "-fl", "prism_live_sync_all.py"],
        text=True,
        capture_output=True,
        check=False,
    )
    lines = proc.stdout.splitlines()
    if script.name == "prism_live_sync_manifest.py":
        proc = subprocess.run(
            ["/usr/bin/pgrep", "-fl", "prism_live_sync_manifest.py"],
            text=True,
            capture_output=True,
            check=False,
        )
        lines.extend(proc.stdout.splitlines())
    if manifest is None:
        return any(needle in line for line in lines)
    manifest_text = str(manifest)
    return any(needle in line and manifest_text in line for line in lines)


def start_live_sync(manifest: Path | None = None) -> None:
    script = MANIFEST_SYNC if manifest is not None else LIVE_SYNC
    if is_live_sync_running(script, manifest):
        log("live sync already running")
        return
    args = [sys.executable, str(script)]
    if manifest is not None:
        args += ["--manifest", str(manifest)]
    else:
        args += ["--interval", "2"]
    stdout = (BASE / "prism_live_sync_all.autostart.log").open("ab")
    subprocess.Popen(
        args,
        cwd=str(BASE),
        stdout=stdout,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    log("started live sync" + (f" for {manifest}" if manifest else ""))


def source_from_url(raw_url: str) -> Path | None:
    parsed = urlparse(raw_url)
    qs = parse_qs(parsed.query)
    value = qs.get("path", [""])[0]
    if not value and parsed.path:
        value = parsed.path
    value = unquote(value)
    if not value:
        return None
    return Path(value)


def manifest_from_url(raw_url: str) -> Path | None:
    parsed = urlparse(raw_url)
    qs = parse_qs(parsed.query)
    value = qs.get("manifest", [""])[0]
    value = unquote(value)
    return Path(value).expanduser().resolve() if value else None


def open_prism_source(source: Path) -> None:
    if not source.exists():
        log(f"source missing: {source}")
        return
    subprocess.Popen(
        ["/usr/bin/open", str(source)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )
    log(f"opened source: {source}")


def main(argv: list[str]) -> int:
    raw_url = argv[1] if len(argv) > 1 else "prismbridge://start"
    log(f"received: {raw_url}")
    manifest = manifest_from_url(raw_url)
    start_live_sync(manifest)
    source = source_from_url(raw_url)
    if source is not None:
        open_prism_source(source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
