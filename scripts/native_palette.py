"""Read/write Prism 11 native schemes, not the exported macOS color-panel .clr cache."""

from pathlib import Path
import hashlib
import fcntl
import os
import plistlib
import re
import tempfile
import windows_prism_executor


def _windows_only():
    try:
        return windows_prism_executor.load_policy(Path(__file__).resolve().parents[1])['backend']=='windows_vm'
    except (OSError,ValueError,TypeError):
        return True  # Invalid machine policy never enables Mac scheme writes.

BUILTIN_ROOT = Path("/Applications/Prism 11.app/Contents/Resources/ColorSchemes")
PALETTE_ROOT = (
    Path.home() / "Library/Application Support/GraphPad/Prism/Common/ColorSchemes"
)


def validate_hex(value):
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError(f"Expected an RGB HEX color such as #3478A6, got {value!r}")
    return value.upper()


def _node(archive):
    return archive["$objects"][archive["$top"]["root"].data]


def _entries(root):
    p = root / "ColorSchemeIdentifiers.plist"
    return plistlib.loads(p.read_bytes()) if p.exists() else []


def resolve_palette(name, root=None):
    if not isinstance(name, str) or any(
        c in name for c in ("/", "\\", '"', "\n", "\r", "%")
    ):
        raise ValueError("Palette must be a registered scheme name, not a path")
    matches = []
    for directory in ([root] if root else [PALETTE_ROOT, BUILTIN_ROOT]):
        for e in _entries(directory):
            p = directory / e["FileName"]
            if (
                p.name.lstrip("*").casefold() == name.lstrip("*").casefold()
                and p.is_file()
            ):
                matches.append(p)
    if len(matches) != 1:
        raise ValueError(
            f"No unique registered Prism color scheme {name!r}; call prism_list_palettes"
        )
    return matches[0]


def read_palette(path):
    node = _node(plistlib.loads(Path(path).read_bytes()))
    result = {}
    for key, val in node.items():
        if (
            key.startswith("graphDataSet")
            and key.endswith("ColorWithAlpha")
            and isinstance(val, bytes)
        ):
            role = key[len("graphDataSet") : -len("ColorWithAlpha")]
            role = {
                "BarFill": "Bar Fill",
                "BarPattern": "Bar Pattern",
                "RowTitle": "Row Title",
            }.get(role, role)
            for i in range(0, len(val), 4):
                result[f"Data Set {role} Color {i//4+1}"] = (
                    "#" + val[i : i + 3].hex().upper()
                )
        elif key in {"graphAxisColorWithAlpha", "graphTitleColorWithAlpha"}:
            rgb = (val & 0xFFFFFFFF).to_bytes(4, "little")[:3]
            result[
                "Axis Color" if key.startswith("graphAxis") else "Graph Title Color"
            ] = ("#" + rgb.hex().upper())
    return result


def list_palettes():
    if _windows_only():
        return []  # Mac plist schemes are not Windows Prism scheme registrations.
    result = []
    for root in [PALETTE_ROOT, BUILTIN_ROOT]:
        for entry in _entries(root):
            p = root / entry["FileName"]
            if not p.is_file() or p.name.startswith("PrismBridge-"):
                continue
            try:
                colors = read_palette(p)
                result.append(
                    {
                        "name": p.name,
                        "display_name": p.name.lstrip("*"),
                        "group_colors": [
                            v
                            for k, v in colors.items()
                            if re.fullmatch(r"Data Set Fill Color \d+", k)
                        ],
                        "axis_color": colors.get("Axis Color"),
                    }
                )
            except (ValueError, KeyError, plistlib.InvalidFileException):
                continue
    return result


def _atomic_write(path, data):
    fd, temp = tempfile.mkstemp(prefix=".prism-scheme-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def compile_palette(
    headers, group_colors, *, base_name="Colorblind Safe", output_root=None
):
    if not headers or len(set(headers)) != len(headers):
        raise ValueError("Native group colors require nonempty, unique group names")
    if not isinstance(group_colors, dict) or set(group_colors) != set(headers):
        raise ValueError(
            "group_colors must specify exactly every group name in the data"
        )
    normalized = {k: validate_hex(v) for k, v in group_colors.items()}
    archive = plistlib.loads(resolve_palette(base_name).read_bytes())
    node = _node(archive)
    colors = [bytes.fromhex(normalized[h][1:]) for h in headers]
    for key in list(node):
        if key.startswith("graphDataSet") and "Color" in key:
            values = (
                [b"\0\0\0"] * len(headers)
                if any(v in key for v in ("Legend", "RowTitle"))
                else colors
            )
            node[key] = b"".join(
                v + (b"\xff" if key.endswith("WithAlpha") else b"\0") for v in values
            )
        elif key.startswith("graph") and "Color" in key and isinstance(node[key], int):
            white = any(
                v in key
                for v in ("Background", "PlottingArea", "ObjectFill", "TableFill")
            )
            raw = (b"\xff\xff\xff" if white else b"\0\0\0") + (
                b"\xff" if key.endswith("WithAlpha") else b"\0"
            )
            node[key] = int.from_bytes(raw, "little", signed=True)
    node["RepeatCount"] = len(headers)
    name = (
        "PrismBridge-"
        + hashlib.sha256(plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)).hexdigest()[
            :16
        ]
    )
    node["Name"] = node["NameUTF8"] = name.encode("utf-8")
    directory = output_root or PALETTE_ROOT
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / name
    with (directory / ".prism-bridge.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not destination.exists():
            _atomic_write(destination, plistlib.dumps(archive, fmt=plistlib.FMT_BINARY))
        entries = _entries(directory)
        if not any(e["FileName"] == name for e in entries):
            entries.append(
                {
                    "FileName": name,
                    "Identifier": max([4999] + [e["Identifier"] for e in entries]) + 1,
                }
            )
            _atomic_write(
                directory / "ColorSchemeIdentifiers.plist",
                plistlib.dumps(entries, fmt=plistlib.FMT_BINARY),
            )
    return {
        "name": name,
        "path": str(destination),
        "group_colors": normalized,
        "group_order": list(headers),
        "axis_color": "#000000",
    }


def prepare_palette(headers, palette=None, group_colors=None):
    if _windows_only() and (group_colors is not None or palette not in (None,'','inherit')):
        raise ValueError('Windows named schemes are not yet calibrated. Use template colors or group_colors on a managed template; Mac scheme files are not applied.')
    if group_colors is not None:
        return compile_palette(
            headers,
            group_colors,
            base_name=(
                palette if palette and palette != "inherit" else "Colorblind Safe"
            ),
        )
    if palette and palette != "inherit":
        p = resolve_palette(palette)
        return {"name": p.name, "path": str(p), "group_colors": None}
    return {"name": None, "path": None, "group_colors": None}
