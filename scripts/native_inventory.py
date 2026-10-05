"""Read-only structural inventory of supported legacy Prism template payloads."""

from pathlib import Path
import hashlib
import template_styles


def parse_payload(raw):
    """Parse bounded records without interpreting uncalibrated field values."""
    if not raw.startswith(b"PCFFGRA4"):
        raise ValueError(
            "This native payload version is not supported by the read-only inspector"
        )
    roots, stack = [], []
    i = 8
    while i < len(raw):
        tag = int.from_bytes(raw[i : i + 2], "little")
        if tag & 0xC000 == 0x4000:
            if not stack or (tag & 0x3FFF) != (stack[-1]["tag"] & 0x3FFF):
                raise ValueError(f"Unknown native container boundary at byte {i}")
            stack.pop()
            i += 2
            continue
        if i + 6 > len(raw):
            raise ValueError("Truncated native record")
        size = int.from_bytes(raw[i + 2 : i + 6], "little")
        if i + 6 + size > len(raw):
            raise ValueError("Invalid native record length")
        item = {
            "tag": tag,
            "value": raw[i + 6 : i + 6 + size],
            "children": [],
            "value_offset": i + 6,
        }
        (stack[-1]["children"] if stack else roots).append(item)
        # The terminal color-table record is a leaf despite its high bit.
        if tag & 0x8000 and tag != 0x8003:
            stack.append(item)
        i += 6 + size
    if stack or len(roots) != 1:
        raise ValueError("Incomplete native project structure")
    return roots


def table_storage_roles(raw):
    """Resolve observed PCFFGRA4 Info backing stores by IDs, never by names.

    A root 8008 is a storage object, not necessarily an input data table.
    A root 8038 Info record's direct 006d (20 bytes) identifies its backing
    store in its first uint32. Remaining stores are NOT certified as data:
    callers must join XML IDs and reject analyses/unclaimed objects.
    """
    sheets = parse_payload(raw)[0]['children']

    def indexed(tag):
        result = {}
        for node in sheets:
            if node['tag'] != tag:
                continue
            if len(node['value']) != 2:
                raise ValueError('Unknown native sheet/storage ID encoding')
            identity = int.from_bytes(node['value'], 'little')
            if identity in result:
                raise ValueError('Duplicate native sheet/storage ID')
            result[identity] = node
        return result

    stores, infos = indexed(0x8008), indexed(0x8038)
    bindings, referenced = [], set()
    for identity, info in sorted(infos.items()):
        descriptors = [n for n in info['children'] if n['tag'] == 0x6d]
        if len(descriptors) != 1 or len(descriptors[0]['value']) != 20 or descriptors[0]['children']:
            raise ValueError('Unknown native Info storage reference encoding')
        target = int.from_bytes(descriptors[0]['value'][:4], 'little')
        if target == 0xffffffff:
            # Observed Info sheets may contain constants without referencing
            # a separate storage object. No actual store is excluded here.
            bindings.append({'info_id': identity, 'table_object_id': None})
            continue
        if target not in stores:
            raise ValueError('Dangling native Info storage reference')
        if target in referenced:
            raise ValueError('Shared native Info backing store is unsupported')
        referenced.add(target)
        bindings.append({'info_id': identity, 'table_object_id': target})
    return {
        'native_info_ids': sorted(infos),
        'info_storage_bindings': bindings,
        'info_table_object_ids': sorted(referenced),
        'non_info_table_object_ids': sorted(set(stores) - referenced),
    }


def inventory(path):
    raw, _, _ = template_styles.template_payload(Path(path))
    roots = parse_payload(raw)
    sheets = roots[0]["children"]

    def name(node):
        value = next(
            (c["value"] for c in node["children"] if c["tag"] == 9), b""
        ).rstrip(b"\0")
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.decode("gb18030", errors="replace")

    def named(tag):
        return [name(n) for n in sheets if n["tag"] == tag]

    return {
        "payload_sha256": hashlib.sha256(raw).hexdigest(),
        "graph_names": named(0x8012),
        "analysis_names": named(0x8039),
        "info_names": named(0x8038),
        "layout_names": named(0x8035),
        "native_table_objects": len(named(0x8008)),
    }
