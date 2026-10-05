#!/usr/bin/env python3
"""Check or install the isolated SVG renderer without touching global packages."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


PINS = {"PyMuPDF": "1.28.2", "resvg-py": "0.5.0"}
HOST_VERSION_CODE = "import platform; print(platform.python_version())"
RENDERER_PROBE_CODE = (
    "import importlib.metadata as m,json,platform,sys\n"
    "def v(n):\n"
    " try:return m.version(n)\n"
    " except m.PackageNotFoundError:return None\n"
    "print(json.dumps({'python':platform.python_version(),'PyMuPDF':v('PyMuPDF'),'resvg-py':v('resvg-py'),"
    "'prefix':sys.prefix,'base_prefix':sys.base_prefix,'executable':sys.executable}))"
)


def _call(run, args, timeout=120):
    return run([os.fspath(item) for item in args], capture_output=True, text=True, timeout=timeout)


def _version_tuple(value: object, label: str) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise RuntimeError(f"{label} returned an invalid Python version")
    try:
        parts = tuple(int(part) for part in value.strip().split(".")[:3])
    except ValueError as exc:
        raise RuntimeError(f"{label} returned an invalid Python version") from exc
    if len(parts) < 2:
        raise RuntimeError(f"{label} returned an invalid Python version")
    return (parts + (0, 0, 0))[:3]


def _host_version(interpreter: Path, run) -> tuple[int, int, int]:
    process = _call(run, [interpreter, "-c", HOST_VERSION_CODE])
    if process.returncode:
        raise RuntimeError(f"Unable to run renderer host Python (exit code {process.returncode})")
    return _version_tuple(process.stdout, "Renderer host Python")


def _probe(renderer: Path, run) -> dict:
    process = _call(run, [renderer, "-c", RENDERER_PROBE_CODE])
    if process.returncode:
        raise RuntimeError(f"Unable to inspect isolated renderer (exit code {process.returncode})")
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Isolated renderer returned invalid version metadata") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Isolated renderer returned invalid version metadata")
    _version_tuple(result.get("python"), "Isolated renderer")
    return result


def _satisfied(info: dict) -> bool:
    return all(info.get(package) == version for package, version in PINS.items())


def _validate_renderer_environment(info: dict, destination: Path) -> None:
    for key in ("prefix", "base_prefix", "executable"):
        if not isinstance(info.get(key), str) or not info[key]:
            raise RuntimeError(f"Isolated renderer did not report {key}")
    prefix = Path(info["prefix"]).expanduser()
    base_prefix = Path(info["base_prefix"]).expanduser()
    if prefix.resolve(strict=False) == base_prefix.resolve(strict=False):
        raise RuntimeError("Isolated renderer Python is not a virtual environment")
    if prefix.resolve(strict=False) != destination.resolve(strict=False):
        raise RuntimeError("Isolated renderer prefix does not match the expected .venv")
    executable = Path(os.path.abspath(os.path.expanduser(info["executable"])))
    prefix_lexical = Path(os.path.abspath(os.path.expanduser(info["prefix"])))
    try:
        contained = Path(os.path.commonpath([executable, prefix_lexical])) == prefix_lexical
    except ValueError:
        contained = False
    if not contained:
        raise RuntimeError("Isolated renderer executable is not lexically within its virtual environment")


def setup_renderer(skill_root, *, check_only=False, python_executable=None, run=subprocess.run) -> dict:
    """Check exact pins or create/update only ``skill_root/.venv``."""
    root = Path(os.path.abspath(os.path.expanduser(os.fspath(skill_root))))
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError(f"Skill root is not a regular directory: {root}")
    destination = root / ".venv"
    renderer = destination / "bin/python"
    created = False
    if not renderer.exists():
        if check_only:
            return {"status": "missing", "changed": False, "destination": os.fspath(destination), "required": dict(PINS)}
        host = Path(os.path.abspath(os.path.expanduser(os.fspath(python_executable or sys.executable))))
        if not host.is_file() or not os.access(host, os.X_OK):
            raise RuntimeError(f"Renderer host Python is not executable: {host}")
        if _host_version(host, run) < (3, 10, 0):
            raise RuntimeError("Creating the isolated renderer requires Python 3.10 or newer; the MCP interpreter may remain on Python 3.9")
        process = _call(run, [host, "-m", "venv", destination], timeout=300)
        if process.returncode:
            raise RuntimeError(f"Unable to create isolated renderer (exit code {process.returncode})")
        if not renderer.exists():
            raise RuntimeError("Renderer environment creation returned success but bin/python is missing")
        created = True

    info = _probe(renderer, run)
    _validate_renderer_environment(info, destination)
    if _version_tuple(info.get("python"), "Isolated renderer") < (3, 10, 0):
        raise RuntimeError("The isolated renderer requires Python 3.10 or newer; recreate only its .venv")
    if _satisfied(info):
        return {"status": "satisfied", "changed": created, "destination": os.fspath(destination), "versions": info, "required": dict(PINS)}
    if check_only:
        return {"status": "needs-install", "changed": False, "destination": os.fspath(destination), "versions": info, "required": dict(PINS)}
    install = _call(run, [renderer, "-m", "pip", "install", "PyMuPDF==1.28.2", "resvg-py==0.5.0"], timeout=600)
    if install.returncode:
        raise RuntimeError(f"Renderer package installation failed (exit code {install.returncode})")
    verified = _probe(renderer, run)
    if _version_tuple(verified.get("python"), "Isolated renderer") < (3, 10, 0) or not _satisfied(verified):
        raise RuntimeError("Renderer installation completed but exact pinned versions were not verified")
    return {"status": "installed", "changed": True, "destination": os.fspath(destination), "versions": verified, "required": dict(PINS)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--python", dest="python_executable")
    parser.add_argument("--check", action="store_true", help="report status without creating or installing")
    args = parser.parse_args(argv)
    try:
        result = setup_renderer(args.skill_root, check_only=args.check, python_executable=args.python_executable)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Renderer setup failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] in {"satisfied", "installed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
