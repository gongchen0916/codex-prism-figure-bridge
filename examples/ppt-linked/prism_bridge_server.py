#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import subprocess
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

LOG_PATH = Path(__file__).resolve().parent / "prism_bridge_server.log"


def log(line: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {line}\n")
    print(f"[{stamp}] {line}", flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        log(f"GET {self.path}")
        if parsed.path == "/refresh":
            self.handle_refresh(parsed)
            return
        if parsed.path != "/open":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><h2>PrismBridge is running.</h2></body></html>")
            return

        qs = parse_qs(parsed.query)
        target = qs.get("path", [""])[0]
        path = Path(target).expanduser()
        ok = path.exists()
        message = ""
        if ok:
            log(f"opening {path}")
            subprocess.Popen(["/usr/bin/open", "-a", "Prism 11", str(path)])
            message = f"Opening in Prism: {path}"
        else:
            log(f"missing {path}")
            message = f"File not found: {path}"

        self.send_response(200 if ok else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>PrismBridge</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,Arial,sans-serif;margin:40px">
<h2>{html.escape(message)}</h2>
<p>You can close this browser tab and return to PowerPoint.</p>
</body></html>"""
        self.wfile.write(body.encode("utf-8"))

    def handle_refresh(self, parsed) -> None:
        qs = parse_qs(parsed.query)
        pptx = Path(qs.get("pptx", [""])[0]).expanduser()
        image = Path(qs.get("image", [""])[0]).expanduser()
        media = qs.get("media", ["ppt/media/image1.png"])[0]
        ok = pptx.exists() and image.exists()
        if ok:
            try:
                replace_pptx_member(pptx, media, image.read_bytes())
                log(f"refreshed {pptx}::{media} from {image}")
                subprocess.Popen(["/usr/bin/open", "-a", "Microsoft PowerPoint", str(pptx)])
                message = f"Refreshed PowerPoint image: {pptx.name}"
            except Exception as exc:
                ok = False
                message = f"Refresh failed: {exc}"
                log(message)
        else:
            message = f"Missing file. pptx={pptx.exists()} image={image.exists()}"
            log(message)

        self.send_response(200 if ok else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>PrismBridge Refresh</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,Arial,sans-serif;margin:40px">
<h2>{html.escape(message)}</h2>
<p>You can close this browser tab and return to PowerPoint.</p>
</body></html>"""
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, fmt: str, *args) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))


def replace_pptx_member(pptx: Path, member: str, data: bytes) -> None:
    tmp = pptx.with_suffix(pptx.suffix + ".tmp")
    backup = pptx.with_suffix(pptx.suffix + ".bak")
    if not backup.exists():
        backup.write_bytes(pptx.read_bytes())
    with zipfile.ZipFile(pptx, "r") as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        found = False
        for item in zin.infolist():
            payload = data if item.filename == member else zin.read(item.filename)
            if item.filename == member:
                found = True
            zout.writestr(item, payload)
        if not found:
            raise ValueError(f"PPTX member not found: {member}")
    tmp.replace(pptx)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    server = HTTPServer((args.host, args.port), Handler)
    log(f"PrismBridgeServer running at http://{args.host}:{args.port}/")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
