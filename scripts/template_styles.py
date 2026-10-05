"""Native color edits restricted to fingerprinted, Prism-calibrated template payloads."""

from pathlib import Path
import base64
import hashlib
import json
import re
import zlib
import native_palette

ROOT = Path(__file__).resolve().parents[1]


def adaptations():
    p = ROOT / "assets/templates/template_adaptations.json"
    return json.loads(p.read_text()) if p.exists() else {}


def template_payload(path):
    text = Path(path).read_text()
    match = re.search(r"(<Template\b[^>]*>)(.*?)(</Template>)", text, re.S)
    if not match:
        raise ValueError("No compressed native template payload")
    try:
        raw = zlib.decompress(base64.b64decode(match.group(2)))
    except (ValueError, zlib.error) as error:
        raise ValueError("Invalid native template payload") from error
    return raw, text, match


def compressed_blocks(raw):
    blocks = []
    for match in re.finditer(b"Zval", raw):
        i = match.start()
        if i < 6:
            continue
        size = int.from_bytes(raw[i + 4 : i + 8], "little")
        raw_size = int.from_bytes(raw[i + 8 : i + 12], "little")
        end = i + 12 + size
        if (
            raw[end : end + 4] != b"Zend"
            or int.from_bytes(raw[i - 4 : i], "little") != size + 16
        ):
            continue
        try:
            inner = zlib.decompress(raw[i + 12 : end])
        except zlib.error:
            continue
        if len(inner) != raw_size:
            raise ValueError("Native extended-style size mismatch")
        blocks.append((i, size, inner))
    return blocks


def validate_colors(headers, colors):
    if (
        len(set(headers)) != len(headers)
        or not isinstance(colors, dict)
        or set(headers) != set(colors)
    ):
        raise ValueError("group_colors must specify exactly every unique group name")
    return {h: native_palette.validate_hex(colors[h]) for h in headers}


def apply_colors(project, alias, headers, colors):
    colors = validate_colors(headers, colors)
    spec = adaptations().get(alias)
    if not spec:
        raise ValueError(
            "Exact HEX colors require a calibrated managed template; use a named native palette for this template"
        )
    if spec["color_binding"] == "trace" and len(set(colors.values())) > 1:
        raise ValueError(
            "A paired trace uses one color across conditions; use the same HEX for each condition"
        )
    raw, text, match = template_payload(project)
    if hashlib.sha256(raw).hexdigest() != spec["payload_sha256"]:
        raise ValueError(
            "Template payload changed since calibration; refusing to edit unknown native bytes"
        )
    modified = bytearray(raw)
    for i, h in enumerate(headers):
        for offset in spec["color_offsets"][i]:
            expected = bytes.fromhex(spec["calibration_colors"][i][1:])
            if raw[offset : offset + 3] != expected:
                raise ValueError("Native color slot fingerprint mismatch")
            modified[offset : offset + 3] = bytes.fromhex(colors[h][1:])
    blocks = {i: (size, inner) for i, size, inner in compressed_blocks(raw)}
    for block in reversed(spec.get("compressed_color_blocks", [])):
        start = block["offset"]
        size, inner = blocks[start]
        if hashlib.sha256(inner).hexdigest() != block["sha256"]:
            raise ValueError("Native extended style changed")
        changed = bytearray(inner)
        for i, h in enumerate(headers):
            for offset in block["color_offsets"][i]:
                expected = bytes.fromhex(spec["calibration_colors"][i][1:])
                if inner[offset : offset + 3] != expected:
                    raise ValueError("Extended color slot mismatch")
                changed[offset : offset + 3] = bytes.fromhex(colors[h][1:])
        packed = zlib.compress(changed)
        replacement = (
            b"Zval"
            + len(packed).to_bytes(4, "little")
            + len(changed).to_bytes(4, "little")
            + packed
            + b"Zend"
        )
        modified[start - 4 : start + 12 + size + 4] = (
            len(replacement).to_bytes(4, "little") + replacement
        )
    encoded = base64.b64encode(zlib.compress(modified)).decode("ascii")
    Path(project).write_text(text[: match.start(2)] + encoded + text[match.end(2) :])
    return {
        "name": None,
        "path": None,
        "group_colors": colors,
        "group_order": headers,
        "method": "calibrated_native_slots",
        "axis_color": "#000000",
    }
