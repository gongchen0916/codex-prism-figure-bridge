#!/usr/bin/env python3
"""Bridge raw data, Prism templates, Prism export scripts, and PowerPoint decks.

The bridge is intentionally file-first. Prism has no full public API, so the
stable path is to mutate Prism XML/template data and let Prism recalculate and
render the graphs.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import csv
import fcntl
import html
import json
import math
import os
import platform
import plistlib
import re
import shutil
import subprocess
import struct
import sys
import tempfile
import time
import threading
import unicodedata
import zipfile
import zlib
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

import template_matcher
import native_palette
import svg_render
import template_styles
import execution_state
import windows_prism_executor
import windows_prism_ppt

ROOT = Path(__file__).resolve().parent
SKILL_ROOT = ROOT.parent
DEFAULT_PRISM_APP = Path("/Applications/Prism 11.app")
DEFAULT_TEMPLATE_ROOT = DEFAULT_PRISM_APP / "Contents/SharedSupport/Portfolio"
SKILL_TEMPLATE_ROOT = SKILL_ROOT / "assets/templates"
SKILL_TEMPLATE_INDEX = SKILL_TEMPLATE_ROOT / "template_index.json"
STAGING_ROOT = Path("/private/tmp/prism_bridge_runs")
PRISM_LOCK_PATH = STAGING_ROOT / ".prism_export.lock"
_LOCK_CONTEXT = threading.local()


def execution_target():
    """Read the user's Windows-only policy. Missing/corrupt policy blocks execution."""
    path=SKILL_ROOT/'assets/prism_execution.json'
    result={'backend':'windows_vm','macos_execution_allowed':False,'ready':False,
            'phase':'policy_missing_or_invalid','reason':'Windows Prism execution pending configuration and native acceptance.'}
    try:
        value=windows_prism_executor.load_policy(SKILL_ROOT)
        result.update(value)
    except (OSError,ValueError,TypeError,KeyError)as exc:result['policy_error']=str(exc)
    return result


def _require_macos_execution():
    target=execution_target()
    raise RuntimeError(target['reason']+' macOS Prism is disabled by the user; install Prism in the Windows VM and complete the Windows executor/PPT acceptance.')


def _require_native_execution():
    target = execution_target()
    if not target['ready']:
        raise RuntimeError(target['reason'] + ' macOS Prism remains disabled.')
    return target
MAX_FAILED_STAGES = 12
UNSAFE_AUTOMATION_TEMPLATE_STEMS = {
    "volcano plot",
    "spaghetti plot",
    "scatter plot with bars",
    "box and whiskers graph",
    "box and whiskers with asterisks",
    "before-after",
    "before-after with error",
    "points and grouped bars",
    "combine points and bars",
    "odds ratio (forest plot)",
    "bland-altman plot",
    "xy frequency distribution",
    "replicates with connected means",
}
PPTX_NS = {
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}

TEMPLATE_ALIASES = {
    "column-scatter": "Graphs to explore/Column scatter.pzt",
    "box-whisker": "Graphs to explore/Box and whiskers graph.pzt",
    "box-whisker-asterisks": "Graphs to explore/Box and whiskers with asterisks.pzt",
    "before-after": "Graphs to explore/Before-after.pzt",
    "before-after-error": "Graphs with tutorials/Before-after with error.pzt",
    "volcano": "Graphs to explore/Volcano plot.pzt",
    "grouped-bars": "Graphs with tutorials/Grouped graph spacing.pzt",
    "points-grouped-bars": "Graphs with tutorials/Points and grouped bars.pzt",
    "dose-response": "Graphs with tutorials/Dose-response curves.pzt",
    "scatter-bars": "Graphs to explore/Scatter plot with bars.pzt",
    "spaghetti": "Graphs to explore/Spaghetti plot.pzt",
}


def qname(tag: str, ns: str | None) -> str:
    return f"{{{ns}}}{tag}" if ns else tag


def namespace(root: ET.Element) -> str | None:
    if root.tag.startswith("{"):
        return root.tag[1:].split("}", 1)[0]
    return None


def read_table(path: Path) -> list[list[str]]:
    text = path.read_text(encoding="utf-8-sig")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel_tab if "\t" in sample else csv.excel
    rows = [
        [cell.strip() for cell in row] for row in csv.reader(text.splitlines(), dialect)
    ]
    rows = [row for row in rows if any(cell != "" for cell in row)]
    if not rows:
        raise ValueError(f"No data rows found in {path}")
    return rows


def split_header(
    rows: list[list[str]], first_row_is_data: bool | None = None
) -> tuple[list[str], list[list[str]]]:
    return template_matcher.split_header(rows, first_row_is_data)


def default_axis_titles(
    data: Path,
    first_row_is_data: bool | None = None,
    hint: str = "",
) -> tuple[str | None, str]:
    rows = read_table(data)
    headers, _ = split_header(rows, first_row_is_data)
    profile = template_matcher.infer_data_profile(
        rows, hint_text=hint, first_row_is_data=first_row_is_data
    )
    if profile["has_x_column"]:
        return headers[0], headers[1]
    return None, "Value"


def numeric_axis_limits(
    rows: list[list[str]],
    first_row_is_data: bool | None = None,
    has_x_column: bool = False,
) -> tuple[float, float, float]:
    _, body = split_header(rows, first_row_is_data)
    values = [
        float(value)
        for row in body
        for value in (row[1:] if has_x_column else row)
        if value != ""
    ]
    if not values:
        raise ValueError("Cannot derive axis limits without numeric data values.")
    low = min(values)
    high = max(values)
    if low == high:
        padding = max(abs(low) * 0.1, 1.0)
    else:
        padding = (high - low) * 0.08
    raw_bottom = low - padding
    raw_top = high + padding
    rough_interval = max((raw_top - raw_bottom) / 4.0, 1e-12)
    magnitude = 10 ** math.floor(math.log10(rough_interval))
    scaled = rough_interval / magnitude
    nice_scaled = 1 if scaled <= 1 else 2 if scaled <= 2 else 5 if scaled <= 5 else 10
    interval = nice_scaled * magnitude
    bottom = math.floor(raw_bottom / interval) * interval
    top = math.ceil(raw_top / interval) * interval
    return bottom, top, interval


def numeric_x_limits(rows, first_row_is_data=None):
    """Keep a linear XY axis close to the observed domain, with marker clearance."""
    _, body = split_header(rows, first_row_is_data)
    if any(row and row[0] == "" and any(v != "" for v in row[1:]) for row in body):
        raise ValueError("XY observations have a missing X value")
    values = [float(row[0]) for row in body if row and row[0] != ""]
    if not values or not all(math.isfinite(v) for v in values):
        raise ValueError("Cannot derive a finite X domain")
    low, high = min(values), max(values)
    pad = (high - low) * 0.03 if high > low else max(abs(low) * 0.03, 1)
    rough = (high - low + 2 * pad) / 5
    magnitude = 10 ** math.floor(math.log10(rough))
    interval = next(v for v in (1, 2, 2.5, 5, 10) if v >= rough / magnitude) * magnitude
    return low - pad, high + pad, interval


def validate_numeric_data(
    rows: list[list[str]], first_row_is_data: bool | None = None
) -> None:
    headers, body = split_header(rows, first_row_is_data)
    header_present = template_matcher.has_header_row(rows, first_row_is_data)
    first_data_row = 2 if header_present else 1
    for body_index, row in enumerate(body):
        row_number = first_data_row + body_index
        for column_index, value in enumerate(row):
            if value == "":
                continue
            try:
                numeric_value = float(value)
            except ValueError as exc:
                header = (
                    headers[column_index]
                    if column_index < len(headers) and headers[column_index]
                    else f"Column {column_index + 1}"
                )
                raise ValueError(
                    f"Non-numeric value in column {header!r} at row {row_number}: {value!r}. "
                    "Prism automation requires wide numeric data: put each group in its own "
                    "numeric column, or convert category/time labels to numeric values."
                ) from exc
            if not math.isfinite(numeric_value):
                header = (
                    headers[column_index]
                    if column_index < len(headers) and headers[column_index]
                    else f"Column {column_index + 1}"
                )
                raise ValueError(
                    f"Non-finite numeric value in column {header!r} at row "
                    f"{row_number}: {value!r}. Use an empty cell for missing data; "
                    "NaN and infinity cannot be sent to Prism automation."
                )


def validate_table_shape(
    rows: list[list[str]], first_row_is_data: bool | None = None
) -> None:
    expected_columns = len(rows[0])
    for row_index, row in enumerate(rows, start=1):
        if len(row) != expected_columns:
            raise ValueError(
                f"Inconsistent column count at row {row_index}: expected "
                f"{expected_columns}, found {len(row)}. Remove extra delimiters or "
                "restore missing cells before Prism automation."
            )

    headers, body = split_header(rows, first_row_is_data)
    if not body:
        raise ValueError(
            "Table must contain at least one data row; no observations were found."
        )
    if template_matcher.has_header_row(rows, first_row_is_data):
        for column_index, header in enumerate(headers, start=1):
            if header == "":
                raise ValueError(
                    f"Detected an empty column header at column {column_index}; "
                    "this usually indicates a trailing delimiter. Remove the empty column."
                )
    for column_index in range(expected_columns):
        values = [row[column_index] for row in body]
        if values and all(value == "" for value in values):
            header = headers[column_index] if column_index < len(headers) else ""
            label = repr(header) if header else str(column_index + 1)
            raise ValueError(
                f"Column {label} contains no data values; remove the empty column or trailing delimiter."
            )


def resolve_template(template: str) -> Path:
    candidate = Path(template).expanduser()
    if candidate.exists():
        return candidate.resolve()

    if SKILL_TEMPLATE_INDEX.exists():
        index = json.loads(SKILL_TEMPLATE_INDEX.read_text(encoding="utf-8"))
        entries = sorted(
            index.get("templates", []),
            key=lambda e: (
                e.get("collection") != "verified",
                e.get("kind") != "xml",
                e.get("alias", ""),
            ),
        )
        for entry in entries:
            if template in {
                entry.get("alias"),
                entry.get("name"),
                entry.get("source_name"),
            }:
                indexed = SKILL_TEMPLATE_ROOT / entry["relative_path"]
                if indexed.exists():
                    return indexed.resolve()

    portfolio = SKILL_TEMPLATE_ROOT / "portfolio"
    if template in TEMPLATE_ALIASES:
        candidate = portfolio / TEMPLATE_ALIASES[template]
        if candidate.exists():
            return candidate.resolve()
        candidate = DEFAULT_TEMPLATE_ROOT / TEMPLATE_ALIASES[template]
        if candidate.exists():
            return candidate.resolve()

    raise FileNotFoundError(
        f"Unknown template {template!r}. Use list-templates or prism_match_template."
    )


@lru_cache(maxsize=8)
def _automation_entries_snapshot(root: str, index_bytes: bytes, curated_bytes: bytes) -> dict[Path, dict]:
    curated = json.loads(curated_bytes)
    whitelist = set(curated.get("whitelist_automation", []))
    limits = curated.get("automation_limits", {})
    index = json.loads(index_bytes)
    return {
        (Path(root) / entry["relative_path"]).resolve(): {
            **entry,
            "automation_max_y_series": limits.get(entry.get("alias"), {}).get(
                "max_y_series"
            ),
        }
        for entry in index.get("templates", [])
        if entry.get("alias") in whitelist
    }


@lru_cache(maxsize=8)
def _indexed_entries_snapshot(root: str, index_bytes: bytes) -> dict[Path, dict]:
    index = json.loads(index_bytes)
    return {
        (Path(root) / entry["relative_path"]).resolve(): entry
        for entry in index.get("templates", [])
    }


def automation_template_entries() -> dict[Path, dict]:
    """Reuse parsing, not stale metadata: content and root are cache keys."""
    return deepcopy(_automation_entries_snapshot(
        str(SKILL_TEMPLATE_ROOT.resolve()), SKILL_TEMPLATE_INDEX.read_bytes(),
        (SKILL_TEMPLATE_ROOT / "curated_templates.json").read_bytes(),
    ))


def indexed_template_entries() -> dict[Path, dict]:
    return deepcopy(_indexed_entries_snapshot(
        str(SKILL_TEMPLATE_ROOT.resolve()), SKILL_TEMPLATE_INDEX.read_bytes(),
    ))


# Retain the established maintenance hook without exposing cached mutable maps.
automation_template_entries.cache_clear = _automation_entries_snapshot.cache_clear
indexed_template_entries.cache_clear = _indexed_entries_snapshot.cache_clear


def is_zip(path: Path) -> bool:
    return zipfile.is_zipfile(path)


def first_table(root: ET.Element, ns: str | None) -> ET.Element:
    table = root.find(f".//{qname('Table', ns)}")
    if table is None:
        raise ValueError("No <Table> found in template")
    return table


def clear_children(parent: ET.Element, names: Iterable[str], ns: str | None) -> None:
    wanted = {qname(name, ns) for name in names}
    for child in list(parent):
        if child.tag in wanted:
            parent.remove(child)


def make_d(value: str, ns: str | None) -> ET.Element:
    d = ET.Element(qname("d", ns))
    d.text = value
    return d


def make_y_column(
    title: str, values: list[str], template: ET.Element | None, ns: str | None
) -> ET.Element:
    if template is not None:
        col = deepcopy(template)
        clear_children(col, ["Title", "Subcolumn"], ns)
    else:
        col = ET.Element(qname("YColumn", ns), {"Width": "70", "Subcolumns": "1"})
    col.set("Subcolumns", "1")
    title_el = ET.Element(qname("Title", ns))
    title_el.text = title
    sub = ET.Element(qname("Subcolumn", ns))
    for value in values:
        sub.append(make_d(value, ns))
    col.append(title_el)
    col.append(sub)
    return col


def y_column_xml(
    title: str, values: list[str], attrs: str = 'Width="70" Decimals="0" Subcolumns="1"'
) -> str:
    cells = "\n".join(f"<d>{html.escape(value)}</d>" for value in values)
    return (
        f"<YColumn {attrs}>\n"
        f"<Title>{html.escape(title)}</Title>\n"
        "<Subcolumn>\n"
        f"{cells}\n"
        "</Subcolumn>\n"
        "</YColumn>"
    )


def sanitize_project_metadata(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    project_created = time.strftime("%b-%d-%Y")

    def scrub_compressed_template(match: re.Match[str]) -> str:
        encoded = re.sub(r"\s+", "", match.group(2))
        try:
            payload = zlib.decompress(base64.b64decode(encoded))
        except (ValueError, zlib.error):
            return match.group(0)

        replacements = (
            ("Experiment Date", "Project Created"),
            ("Mar-10-2011", project_created),
        )
        scrubbed = payload
        for stale, replacement in replacements:
            if len(stale) != len(replacement):
                raise ValueError(
                    "Prism template metadata replacements must preserve length"
                )
            for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
                scrubbed = scrubbed.replace(
                    stale.encode(encoding), replacement.encode(encoding)
                )
        stale_filetime = bytes.fromhex("00 c0 4b 19 b6 de cb 01")
        current_filetime = int((time.time() + 11_644_473_600) * 10_000_000).to_bytes(
            8, "little"
        )
        scrubbed = scrubbed.replace(stale_filetime, current_filetime)
        if scrubbed == payload:
            return match.group(0)

        compressed = zlib.compress(scrubbed)
        base64_text = base64.b64encode(compressed).decode("ascii")
        wrapped = "\n".join(
            base64_text[offset : offset + 76]
            for offset in range(0, len(base64_text), 76)
        )
        return f"<Template{match.group(1)}>{wrapped}</Template>"

    text = re.sub(
        r"<Template\b([^>]*)>(.*?)</Template>",
        scrub_compressed_template,
        text,
        flags=re.S,
    )
    text = re.sub(
        r"<Constant\b[^>]*>\s*<Name\b[^>]*>\s*"
        r"(?:Experiment Date|Project Created)\s*</Name>.*?</Constant>\s*",
        "",
        text,
        flags=re.S,
    )
    text = re.sub(r"<FloatingNote\b[^>]*>.*?</FloatingNote>\s*", "", text, flags=re.S)
    text = re.sub(r"<Created>.*?</Created>\s*", "", text, flags=re.S)
    text = re.sub(r"<Notes\b[^>]*>.*?</Notes>", "<Notes></Notes>", text, flags=re.S)
    text = re.sub(r"<Value\b[^>]*>.*?</Value>", "<Value></Value>", text, flags=re.S)
    path.write_text(text, encoding="utf-8")


def patch_column_template_preserving(
    template_path: Path,
    data_path: Path,
    out: Path,
    title: str | None = None,
    *,
    first_row_is_data: bool | None = None,
) -> None:
    text = template_path.read_text(encoding="utf-8", errors="replace")
    if "<XColumn" in text:
        raise ValueError(
            "Template-preserving patcher supports column templates without XColumn only."
        )

    rows = read_table(data_path)
    headers, body = split_header(rows, first_row_is_data)

    table_matches = list(re.finditer(r"<Table\b[^>]*>.*?</Table>", text, flags=re.S))
    if len(table_matches) != 1:
        raise ValueError(
            f"Column template contains multiple data tables ({len(table_matches)}); "
            "automatic table selection would be ambiguous. Pick a single-table template."
        )
    table_match = table_matches[0]
    table_text = table_match.group(0)

    if title:
        title_match = re.search(r"<Title>.*?</Title>", table_text, flags=re.S)
        if title_match:
            table_text = (
                table_text[: title_match.start()]
                + f"<Title>{html.escape(title)}</Title>"
                + table_text[title_match.end() :]
            )

    y_matches = list(
        re.finditer(r"<YColumn\b[^>]*>.*?</YColumn>", table_text, flags=re.S)
    )
    if not y_matches:
        raise ValueError(f"No YColumn blocks found in template: {template_path}")
    first_attrs_match = re.match(
        r"<YColumn\s+([^>]*)>", y_matches[0].group(0), flags=re.S
    )
    attrs = (
        first_attrs_match.group(1)
        if first_attrs_match
        else 'Width="70" Decimals="0" Subcolumns="1"'
    )

    columns = []
    for index, header in enumerate(headers):
        values = [row[index] if len(row) > index else "" for row in body]
        columns.append(y_column_xml(header or f"Y{index + 1}", values, attrs))
    replacement = "\n".join(columns)
    table_text = (
        table_text[: y_matches[0].start()]
        + replacement
        + table_text[y_matches[-1].end() :]
    )
    text = text[: table_match.start()] + table_text + text[table_match.end() :]

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    sanitize_project_metadata(out)


def replace_subcolumn_values(
    column: ET.Element, values: list[str], ns: str | None
) -> None:
    subcolumns = list(column.findall(qname("Subcolumn", ns)))
    if subcolumns:
        subcolumn = subcolumns[0]
        for extra in subcolumns[1:]:
            column.remove(extra)
        clear_children(subcolumn, ["d"], ns)
    else:
        subcolumn = ET.Element(qname("Subcolumn", ns))
        column.append(subcolumn)
    for value in values:
        subcolumn.append(make_d(value, ns))
    column.set("Subcolumns", "1")


def set_column_title(column: ET.Element, title: str, ns: str | None) -> None:
    title_el = column.find(qname("Title", ns))
    if title_el is None:
        title_el = ET.Element(qname("Title", ns))
        column.insert(0, title_el)
    for child in list(title_el):
        title_el.remove(child)
    title_el.text = title


def replace_legacy_xml_data(
    template: Path,
    data: Path,
    out: Path,
    title: str | None = None,
    *,
    first_row_is_data: bool | None = None,
) -> None:
    rows = read_table(data)
    headers, body = split_header(rows, first_row_is_data)
    tree = ET.parse(template)
    root = tree.getroot()
    ns = namespace(root)
    if ns:
        ET.register_namespace("", ns)
    ET.register_namespace("dt", "urn:schemas-microsoft-com:datatypes")

    tables = list(root.findall(f".//{qname('Table', ns)}"))
    table = next(
        (item for item in tables if item.find(qname("XColumn", ns)) is not None), None
    )
    if table is None:
        raise ValueError("XY template contains no table with a direct XColumn")

    if len(headers) < 2:
        raise ValueError("XY templates need at least one X column and one Y column")

    title_el = table.find(qname("Title", ns))
    if title and title_el is not None:
        title_el.text = title

    y_template = table.find(qname("YColumn", ns))
    for y in list(table.findall(qname("YColumn", ns))):
        table.remove(y)

    x_values = [row[0] if len(row) > 0 else "" for row in body]
    x_col = table.find(qname("XColumn", ns))
    if x_col is None:
        raise ValueError("Selected XY table has no XColumn")
    replace_subcolumn_values(x_col, x_values, ns)
    set_column_title(x_col, headers[0], ns)

    x_advanced = table.find(qname("XAdvancedColumn", ns))
    if x_advanced is not None:
        replace_subcolumn_values(x_advanced, x_values, ns)
        set_column_title(x_advanced, headers[0], ns)

    table.set("YFormat", "replicates")
    table.set("Replicates", "1")
    for index, header in enumerate(headers[1:], start=1):
        values = [row[index] if len(row) > index else "" for row in body]
        table.append(make_y_column(header or f"Y{index}", values, y_template, ns))

    out.parent.mkdir(parents=True, exist_ok=True)
    tree.write(out, encoding="UTF-8", xml_declaration=True)
    sanitize_project_metadata(out)


def parse_long_twoway_data(
    data: Path,
) -> tuple[list[str], list[str], dict[tuple[str, str], list[str]]]:
    rows = read_table(data)
    if len(rows) < 2:
        raise ValueError(
            "Long TwoWay data requires a header and at least one observation."
        )
    header = [re.sub(r"[^a-z0-9]+", "", cell.lower()) for cell in rows[0]]
    required = {"row", "group", "value"}
    if set(header) != required or len(header) != 3:
        raise ValueError(
            "Long TwoWay data must have exactly these columns: row,group,value."
        )
    row_index = header.index("row")
    group_index = header.index("group")
    value_index = header.index("value")
    row_titles: list[str] = []
    groups: list[str] = []
    values: dict[tuple[str, str], list[str]] = {}
    for line_number, record in enumerate(rows[1:], start=2):
        if len(record) != 3:
            raise ValueError(
                f"Inconsistent column count at row {line_number}: expected 3, found {len(record)}."
            )
        row_title = record[row_index]
        group = record[group_index]
        value = record[value_index]
        if not row_title or not group or not value:
            raise ValueError(
                f"Long TwoWay row {line_number} contains an empty row/group/value."
            )
        try:
            numeric = float(value)
        except ValueError as exc:
            raise ValueError(
                f"Long TwoWay value at row {line_number} is not numeric: {value!r}."
            ) from exc
        if not math.isfinite(numeric):
            raise ValueError(
                f"Long TwoWay value at row {line_number} is not finite: {value!r}."
            )
        if row_title not in row_titles:
            row_titles.append(row_title)
        if group not in groups:
            groups.append(group)
        values.setdefault((row_title, group), []).append(value)
    return row_titles, groups, values


def long_twoway_axis_limits(data: Path) -> tuple[float, float, float]:
    _, _, values = parse_long_twoway_data(data)
    numeric_rows = [[value for cell_values in values.values() for value in cell_values]]
    return numeric_axis_limits(numeric_rows, first_row_is_data=True)


def patch_twoway_template(
    template: Path,
    data: Path,
    out: Path,
    title: str | None = None,
) -> tuple[int, int]:
    row_titles, groups, values = parse_long_twoway_data(data)
    tree = ET.parse(template)
    root = tree.getroot()
    ns = namespace(root)
    if ns:
        ET.register_namespace("", ns)
    ET.register_namespace("dt", "urn:schemas-microsoft-com:datatypes")
    tables = [
        item
        for item in root.findall(f".//{qname('Table', ns)}")
        if item.get("TableType") == "TwoWay"
    ]
    if len(tables) != 1:
        raise ValueError("TwoWay preview requires exactly one TwoWay data table.")
    table = tables[0]
    row_column = table.find(qname("RowTitlesColumn", ns))
    if row_column is None:
        raise ValueError("TwoWay template has no RowTitlesColumn.")
    row_subcolumn = row_column.find(qname("Subcolumn", ns))
    if row_subcolumn is None:
        row_subcolumn = ET.SubElement(row_column, qname("Subcolumn", ns))
    row_capacity = len(row_subcolumn.findall(qname("d", ns)))
    if len(row_titles) > row_capacity:
        raise ValueError(
            f"TwoWay template supports at most {row_capacity} rows, received {len(row_titles)}."
        )
    clear_children(row_subcolumn, ["d"], ns)
    for row_title in row_titles:
        row_subcolumn.append(make_d(row_title, ns))

    existing_columns = list(table.findall(qname("YColumn", ns)))
    if len(groups) > len(existing_columns):
        raise ValueError(
            f"TwoWay template supports at most {len(existing_columns)} groups, received {len(groups)}."
        )
    max_replicates = max(len(cell) for cell in values.values())
    for column in existing_columns:
        table.remove(column)
    for group_index, group in enumerate(groups):
        column = deepcopy(existing_columns[group_index])
        clear_children(column, ["Title", "Subcolumn"], ns)
        title_element = ET.Element(qname("Title", ns))
        title_element.text = group
        column.append(title_element)
        for replicate_index in range(max_replicates):
            subcolumn = ET.Element(qname("Subcolumn", ns))
            for row_title in row_titles:
                cell_values = values.get((row_title, group), [])
                value = (
                    cell_values[replicate_index]
                    if replicate_index < len(cell_values)
                    else ""
                )
                subcolumn.append(make_d(value, ns))
            column.append(subcolumn)
        column.set("Subcolumns", str(max_replicates))
        table.append(column)
    table.set("YFormat", "replicates")
    table.set("Replicates", str(max_replicates))
    table_title = table.find(qname("Title", ns))
    if title and table_title is not None:
        table_title.text = title
    out.parent.mkdir(parents=True, exist_ok=True)
    tree.write(out, encoding="UTF-8", xml_declaration=True)
    sanitize_project_metadata(out)
    return len(row_titles), len(groups)


def build_project(
    template: Path,
    data: Path,
    out: Path,
    title: str | None = None,
    hint: str = "",
    first_row_is_data: bool | None = None,
) -> str:
    template = template.resolve()
    curated = template_matcher.load_curated(SKILL_ROOT)
    blocked = template_matcher.unsupported_automation_match(
        hint,
        curated,
    )
    if blocked is not None:
        raise ValueError(
            f"Blocked figure type {blocked.get('id')!r}: {blocked.get('reason')}"
        )
    template_entry = automation_template_entries().get(template)
    if template_entry is None:
        raise ValueError(
            f"{template.name} is not automation-whitelisted. Only templates listed by "
            "prism_list_templates may be patched and rendered automatically."
        )
    adaptation = template_styles.adaptations().get(template_entry["alias"])
    if adaptation:
        import hashlib

        if (
            hashlib.sha256(template.read_bytes()).hexdigest()
            != adaptation["template_sha256"]
        ):
            raise ValueError(
                "Managed template changed after native verification; recalibrate before use"
            )
        if template_entry["alias"] == "watercolor-bars-sem" and re.search(
            r"(?<![a-z])(?:sd|ci)(?![a-z])|standard deviation|标准差|置信区间",
            hint,
            re.I,
        ):
            raise ValueError(
                "This template uses SEM; SD/CI requires a different native graph definition"
            )
        if template_entry["alias"] == "watercolor-box" and re.search(
            r"min(?:imum)?\s*(?:to|-)\s*max|10.90|5.95", hint, re.I
        ):
            raise ValueError(
                "This template uses Tukey whiskers; the requested whisker definition needs a different template"
            )
    if is_zip(template):
        raise ValueError(
            f"{template} is a Prism zip bundle. Pick a legacy XML portfolio template from prism_match_template."
        )
    if template.stem.lower() in UNSAFE_AUTOMATION_TEMPLATE_STEMS:
        raise ValueError(
            f"{template.name} is not automation-safe: its exported graph may remain bound to template "
            "data instead of the patched table. Pick a whitelisted template from prism_match_template."
        )

    rows = read_table(data)
    validate_table_shape(rows, first_row_is_data)
    validate_numeric_data(rows, first_row_is_data)
    profile = template_matcher.infer_data_profile(rows, hint, first_row_is_data)
    compatibility = template_matcher.compatibility_result(
        template_entry, profile, curated.get('automation_limits', {}))
    if not compatibility['compatible']:
        raise ValueError('Template incompatible: ' + ' '.join(compatibility['reasons']))
    if template_entry["alias"] == "watercolor-xy-lines":
        numeric_x_limits(rows, first_row_is_data)
    curated_targets = template_matcher.curated_bonuses(hint, curated, profile)
    matched_figures = template_matcher.curated_hint_matches(hint, curated)
    if matched_figures and not curated_targets:
        expected = sorted(
            {
                template_matcher.normalized_table_type(item.get("table_type"))
                or "unknown"
                for item in matched_figures
            }
        )
        raise ValueError(
            f"Figure hint {hint!r} does not match the supplied data shape. "
            f"It expects {', '.join(expected)} data, but received {profile['table_type']}."
        )
    if curated_targets and template_entry["alias"] not in curated_targets:
        expected = ", ".join(sorted(curated_targets))
        raise ValueError(
            f"Template {template_entry['alias']!r} does not match figure hint {hint!r}. "
            f"Use the curated template: {expected}."
        )
    template_text = template.read_text(encoding="utf-8", errors="replace")
    template_has_x = "<XColumn" in template_text

    if profile["has_x_column"] and not template_has_x:
        raise ValueError(
            "XY data requires a template with an X column. Use prism_match_template to choose an XY template."
        )
    if not profile["has_x_column"] and template_has_x:
        raise ValueError(
            "Column data cannot be used with an XY template because the first group would be consumed as X. "
            "Use a Column template or provide an explicit X header such as Time, Dose, or Concentration."
        )

    requested_y = profile["column_count"] - (1 if profile["has_x_column"] else 0)
    capacity = int(
        template_entry.get("automation_max_y_series")
        or template_entry.get("y_column_count")
        or 0
    )
    if requested_y > capacity:
        raise ValueError(
            f"Template {template_entry['alias']!r} supports at most {capacity} Y "
            f"series/groups, but received {requested_y}. Split the figure or "
            "reduce the number of data columns."
        )

    if not template_has_x:
        patch_column_template_preserving(
            template,
            data,
            out,
            title,
            first_row_is_data=first_row_is_data,
        )
        return "template-preserving column patch"

    replace_legacy_xml_data(
        template,
        data,
        out,
        title,
        first_row_is_data=first_row_is_data,
    )
    return "element-tree xy patch"


def build_unverified_preview_project(
    template: Path,
    data: Path,
    out: Path,
    title: str | None = None,
    hint: str = "",
    first_row_is_data: bool | None = None,
    data_format: str = "wide_numeric",
) -> tuple[str, dict]:
    """Patch a structurally compatible indexed template for manual review only."""
    template = template.resolve()
    entry = indexed_template_entries().get(template)
    if entry is None:
        raise ValueError(
            "Experimental preview accepts only templates from template_index.json."
        )
    if entry.get("kind") != "xml" or entry.get("table_count") != 1:
        raise ValueError(
            f"Template {entry.get('alias')!r} is not preview-patchable: experimental preview "
            "requires one legacy XML data table."
        )
    if entry.get("table_type") not in {"OneWay", "XY", "TwoWay"}:
        raise ValueError(
            f"Template {entry.get('alias')!r} uses unsupported preview table type "
            f"{entry.get('table_type')!r}; only OneWay, XY, and TwoWay are currently patchable."
        )

    if data_format == "long_twoway":
        if entry.get("table_type") != "TwoWay":
            raise ValueError("long_twoway data requires an indexed TwoWay template.")
        patch_twoway_template(template, data, out, title)
        return "unverified long two-way patch", entry
    if data_format != "wide_numeric":
        raise ValueError(f"Unknown preview data format: {data_format!r}.")
    if entry.get("table_type") == "TwoWay":
        raise ValueError("TwoWay templates require data_format='long_twoway'.")

    rows = read_table(data)
    validate_table_shape(rows, first_row_is_data)
    validate_numeric_data(rows, first_row_is_data)
    profile = template_matcher.infer_data_profile(rows, hint, first_row_is_data)
    template_has_x = bool(entry.get("has_x_column"))
    if bool(profile["has_x_column"]) != template_has_x:
        expected = "XY with an explicit X header" if template_has_x else "Column/OneWay"
        raise ValueError(
            f"Template {entry.get('alias')!r} expects {expected} data, but the supplied table "
            f"was classified as {profile['table_type']}."
        )
    requested_y = profile["column_count"] - (1 if profile["has_x_column"] else 0)
    capacity = int(entry.get("y_column_count") or 0)
    if requested_y > capacity:
        raise ValueError(
            f"Template {entry.get('alias')!r} contains {capacity} Y data sets but received "
            f"{requested_y}; experimental preview does not invent graph bindings."
        )

    if template_has_x:
        replace_legacy_xml_data(
            template,
            data,
            out,
            title,
            first_row_is_data=first_row_is_data,
        )
        return "unverified element-tree xy patch", entry
    patch_column_template_preserving(
        template,
        data,
        out,
        title,
        first_row_is_data=first_row_is_data,
    )
    return "unverified template-preserving column patch", entry


def run_osascript(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["osascript", "-e", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def posix_to_hfs(path: Path) -> str:
    # Compatibility name for existing recipe emitters. Windows executor replaces
    # the single SetPath with its unique guest-local stage before dispatch.
    if execution_target()['backend'] == 'windows_vm':
        return str(path.expanduser().resolve())
    if platform.system() != "Darwin":
        return str(path)
    directory_path = path.expanduser().resolve().as_posix().rstrip("/") + "/"
    cp = run_osascript(f"return POSIX file {json.dumps(directory_path)} as text")
    if cp.returncode != 0:
        raise RuntimeError(
            cp.stdout.strip() or f"Unable to convert path for Prism: {path}"
        )
    return cp.stdout.strip()


def read_prism_log(path: Path) -> str:
    if not path.exists():
        return ""
    raw = path.read_bytes()
    def normalize(text):
        return re.sub(r'(?m)^\s*完成[!！]\s*没有错误[。.]\s*$', 'COMPLETE! No Errors.', text.replace('\x00','').strip())
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        return normalize(raw.decode('utf-16', errors='replace'))
    for encoding in ("utf-8-sig", "utf-16-le", "utf-32-le"):
        try:
            text = raw.decode(encoding)
            if text.strip():
                return normalize(text)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace").replace("\x00", "").strip()


@contextmanager
def prism_export_lock(wait_timeout: int = 60) -> Iterable[None]:
    if getattr(_LOCK_CONTEXT,'pid',None)==os.getpid()and getattr(_LOCK_CONTEXT,'depth',0):
        _LOCK_CONTEXT.depth+=1
        try:yield
        finally:_LOCK_CONTEXT.depth-=1
        return
    PRISM_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PRISM_LOCK_PATH.open("a", encoding="utf-8") as lock_file:
        deadline = time.monotonic() + wait_timeout
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for the Prism export lock.")
                time.sleep(0.25)
        try:
            _LOCK_CONTEXT.depth=1
            _LOCK_CONTEXT.pid=os.getpid()
            yield
        finally:
            _LOCK_CONTEXT.depth=0
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _execution_journal():
    return PRISM_LOCK_PATH.with_name(PRISM_LOCK_PATH.name+'.operation.json')


def prism_process_inventory():
    target=execution_target()
    return windows_prism_executor.process_inventory(target) if target['ready'] else {'available':False,'instances':[]}


def execution_status(passive=False):
    target=execution_target()
    if target['ready']and not passive:
        try:
            record=execution_state.read(_execution_journal())
            if record and record['state'] in execution_state.PENDING:
                windows_prism_executor.refresh(record['script'],target)
        except (OSError,ValueError,RuntimeError,subprocess.SubprocessError):
            pass  # Remain fenced when guest status cannot be established.
    return execution_state.status(_execution_journal(),read_prism_log)


def _check_execution_ready(script=None):
    ppt_state=windows_prism_ppt.execution_status()
    if ppt_state['blocked']:
        raise TimeoutError('PPT native execution pending recovery: '+json.dumps(ppt_state,ensure_ascii=False))
    state=execution_status()
    if state.get('late_completion_observed'):
        execution_state.save(_execution_journal(),execution_state.read(_execution_journal()),'completed_late','Completion observed after caller returned; no replay performed.')
        state=execution_status()
    if state['blocked']:
        raise TimeoutError('Prism execution pending recovery: '+json.dumps(state,ensure_ascii=False))
    if script is not None and state['state']=='completed_late'and str(Path(script).resolve())==state['script']:
        raise TimeoutError('completed_late: inspect previous outputs; do not replay the same script. operation_id='+state['operation_id'])


def recover_execution(operation_id):
    with prism_export_lock(wait_timeout=1):
        ppt_state=windows_prism_ppt.execution_status()
        if ppt_state.get('operation_id')==operation_id:
            return windows_prism_ppt.recover(operation_id,execution_target())
        state=execution_status()
        if state.get('operation_id')!=operation_id:raise ValueError('Recovery operation_id does not match the current journal')
        if not state['blocked']:return state
        record=execution_state.read(_execution_journal())
        if execution_state.native_finished(record,read_prism_log):
            execution_state.save(_execution_journal(),record,'completed_late','Verified late completion; original result still needs figure/data verification.')
        elif execution_target()['ready'] and windows_prism_executor.rejected_local_file_proof(record):
            execution_state.save(_execution_journal(),record,'not_started','Unchanged local-file/quoted-SetPath syntax rejected before Windows job creation; no job metadata exists. Native completion and outputs are not claimed.')
        elif execution_target()['ready'] and windows_prism_executor.cancel_not_started(record['script'],execution_target()):
            execution_state.save(_execution_journal(),record,'not_started','Atomic guest claim confirms no dispatch occurred; any late RPC is now prevented from launching Prism.')
        else:
            current=prism_process_inventory()
            if not current['available']:raise RuntimeError('Cannot verify Prism process state; recovery refused')
            if current['instances']:raise RuntimeError('Prism is still running. Save your work and quit Prism before recovering an uncertain execution.')
            if not record['prism_processes']:raise RuntimeError('No original Prism process was observed; native launch outcome needs manual diagnosis, not a blind retry')
            execution_state.save(_execution_journal(),record,'interrupted_process_exit','Prism process exit verified; prior outputs are incomplete/unverified. Use a fresh output directory.')
        return execution_status()


def cleanup_stale_stages(
    max_entries: int = MAX_FAILED_STAGES,
    max_age_seconds: int = 24 * 60 * 60,
) -> None:
    if not STAGING_ROOT.exists():
        return
    operation=execution_status()
    # Preserve every stage if the journal itself is unreadable; never erase
    # the only evidence that can resolve an interrupted native operation.
    if operation['state']=='invalid_journal':return
    protected=Path(operation['script']).parent.resolve()if operation['blocked']else None
    now = time.time()
    stages = [
        path
        for path in STAGING_ROOT.iterdir()
        if path.is_dir() and now - path.stat().st_mtime >= max_age_seconds
    ]
    stages.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    protected_recent = [
        path
        for path in STAGING_ROOT.iterdir()
        if path.is_dir() and now - path.stat().st_mtime < max_age_seconds
    ]
    keep_old = max(0, max_entries - len(protected_recent))
    for old_stage in stages[keep_old:]:
        if protected is not None and(old_stage.resolve()==protected or old_stage.resolve()in protected.parents):continue
        unresolved=False
        for receipt in old_stage.rglob('.prism-operation-*.json'):
            try:
                record=execution_state.read(receipt)
                if record is not None and record['state']not in('completed','not_started'):unresolved=True;break
            except (OSError,ValueError,KeyError,TypeError):unresolved=True;break
        if unresolved:continue
        shutil.rmtree(old_stage, ignore_errors=True)


def cleanup_staging_root(max_failed: int = MAX_FAILED_STAGES) -> None:
    cleanup_stale_stages(max_entries=max_failed, max_age_seconds=10 * 60)


def create_probe_script(outdir: Path) -> Path:
    script = outdir / "_prism_probe_export.pzc"
    outdir_hfs = posix_to_hfs(outdir) if platform.system() == "Darwin" else str(outdir)
    script.write_text(
        "\n".join(
            [
                "CreateLog",
                f'SetPath "{outdir_hfs}"',
                'OpenOutput "probe_done.txt", CLEAR',
                'WText "probe"',
                "CloseOutput",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return script


def run_prism_probe(stage: Path, timeout: int = 10) -> tuple[bool, str]:
    probe = create_probe_script(stage)
    return run_prism_script(probe, timeout=timeout, done_name="probe_done.txt")


def probe_prism(stage: Path, timeout: int = 10) -> tuple[bool, str]:
    return run_prism_probe(stage, timeout=timeout)


def prism_script_text(value: str) -> str:
    if re.search(r'%[A-Za-z0-9]', value):
        raise ValueError('Macro-like percent tokens are unsupported in literal Prism labels; use a space after %')
    return (
        value.replace('"', "'").replace("\r", " ").replace("\n", " ")
    )


def create_export_script(
    project: Path,
    outdir: Path,
    basename: str,
    *,
    keep_prism_warm: bool = False,
    graph_title: str | None = None,
    x_axis_title: str | None = None,
    y_axis_title: str | None = None,
    y_axis_limits: tuple[float, float, float] | None = None,
    x_axis_limits: tuple[float, float, float] | None = None,
    palette_name: str | None = None,
    graph_index: int = 1,
    inline_health_check: bool = False,
) -> Path:
    if type(graph_index) is not int or graph_index < 1:
        raise ValueError("graph_index must be a positive integer")
    script = outdir / f"{basename}_export.pzc"
    outdir_hfs = posix_to_hfs(outdir) if platform.system() == "Darwin" else str(outdir)
    project_name = project.name
    lines = [
        "CreateLog",
        f'SetPath "{outdir_hfs}"',
    ]
    if inline_health_check:
        # Responsiveness evidence is deliberately not the final completion
        # marker. The journal still requires the fresh done/log/SVG evidence.
        lines.extend(['OpenOutput "native_ready.txt", CLEAR',
                      'WText "ready"', 'CloseOutput'])
    lines.extend([f'Open "{project_name}"', f"GoTo G, {graph_index}"])
    if palette_name:
        lines.append(f'ApplyColorScheme "{prism_script_text(palette_name)}"')
    if graph_title is not None:
        lines.append(f'SetGraphTitle "{prism_script_text(graph_title)}"')
    if x_axis_title is not None:
        lines.append(f'SetAxisTitle X, "{prism_script_text(x_axis_title)}"')
    if y_axis_title is not None:
        lines.append(f'SetAxisTitle Y, "{prism_script_text(y_axis_title)}"')
    for axis, limits in (("X", x_axis_limits), ("Y", y_axis_limits)):
        if limits is None:
            continue
        bottom, top, interval = limits
        lines.extend(
            [
                f"SetAxisLimits {axis} bottom {bottom:g}",
                f"SetAxisLimits {axis} top {top:g}",
                f"SetAxisLimits {axis} interval {interval:g}",
            ]
        )
    lines.append("Save")
    lines.append(f'ExportSVG "{basename}.svg"')
    if not keep_prism_warm:
        lines.append("Close")
    lines.extend(
        [
            'OpenOutput "done.txt", CLEAR',
            'WText "done"',
            "CloseOutput",
        ]
    )
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return script


def run_prism_script(
    script: Path, timeout: int = 120, done_name: str = "done.txt"
) -> tuple[bool, str]:
    try:_require_native_execution()
    except RuntimeError as exc:return False,str(exc)
    if not math.isfinite(timeout)or timeout<0:raise ValueError('Finite nonnegative native timeout required')
    if Path(done_name).name!=done_name or done_name in('','.','..'):raise ValueError('Completion marker must be a local filename')
    script=Path(script).resolve()
    if done_name=='done.txt'and not script.name.endswith('_export.pzc'):
        raise ValueError('done.txt is reserved for *_export.pzc SVG exports; use an explicit distinct done_name for other scripts')
    # Pure syntax validation cannot have reached Prism. Reject it before a
    # durable dispatch record is created, rather than fencing a non-operation.
    windows_prism_executor.script_files(script,require_set_path=False)
    windows_prism_executor.validate_inputs(script)
    try:
        with prism_export_lock(wait_timeout=max(1,min(timeout,60))):
            _check_execution_ready(script)
            processes=prism_process_inventory()['instances']
            svg=script.parent/script.name.replace('_export.pzc','.svg')if done_name=='done.txt'else None
            record=execution_state.begin(_execution_journal(),script,script.parent/done_name,svg,processes)
            try:
                ok,message=_run_prism_script_unchecked(script,timeout,done_name)
                if ok and not execution_state.native_finished(record,read_prism_log):ok,message=False,'Native completion could not be bound to unchanged script and fresh artifacts.'
                if ok:state='completed'
                elif message.startswith('Unable to launch Prism'):state='not_started'
                else:state='unknown'
                if state=='unknown'and not record['prism_processes']:record['prism_processes']=prism_process_inventory()['instances']
                execution_state.save(_execution_journal(),record,state,message[-2000:])
                if state=='unknown':message+='\nExecution outcome unknown; no automatic retry. operation_id='+record['operation_id']
                return ok,message
            except subprocess.TimeoutExpired as exc:
                execution_state.save(_execution_journal(),record,'unknown','Launch command timed out; delivery to Prism is uncertain.')
                return False,'Launch timed out; native execution was not cancelled. operation_id='+record['operation_id']
            except BaseException:
                execution_state.save(_execution_journal(),record,'unknown','Controller interrupted; native execution may still be running.')
                raise
    except TimeoutError as exc:return False,str(exc)


def _run_prism_script_unchecked(script,timeout,done_name):
    return windows_prism_executor.run(script,timeout,done_name,_require_native_execution())


def run_prism_export_staged(
    project: Path,
    outdir: Path,
    basename: str,
    *,
    timeout: int = 120,
    keep_prism_warm: bool = False,
    graph_title: str | None = None,
    x_axis_title: str | None = None,
    y_axis_title: str | None = None,
    y_axis_limits: tuple[float, float, float] | None = None,
    x_axis_limits: tuple[float, float, float] | None = None,
    palette_name: str | None = None,
    graph_index: int = 1,
    title_mode: str = "auto",
    clean_metadata: bool = True,
    preflight_mode: str = "inline",
) -> tuple[bool, str, Path]:
    if preflight_mode not in {"separate", "inline"}:
        raise ValueError("preflight_mode must be separate or inline")
    try:_require_native_execution()
    except RuntimeError as exc:return False,str(exc),Path(outdir)/(basename+'.svg')
    if title_mode not in {"auto", "native", "overlay"}:
        raise ValueError("title_mode must be auto, native, or overlay")
    """Run Prism export from a short staging path, then copy SVG/log back.

    Prism's macOS script runner can hang on `Open` when the script directory is
    a long POSIX/HFS path. A short /private/tmp staging directory avoids that
    failure mode while preserving the public output paths.
    """
    final_svg = outdir / f"{basename}.svg"
    final_log = outdir / f"{basename}_export.log"
    started = time.perf_counter()
    preflight_seconds = native_seconds = 0.0
    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    lock_wait_timeout = max(60, timeout + 30)
    try:
        with prism_export_lock(wait_timeout=lock_wait_timeout):
            _check_execution_ready()
            cleanup_staging_root()
            stage = Path(tempfile.mkdtemp(prefix=f"{basename}_", dir=STAGING_ROOT))
            export_ok = False
            log_text = ""
            try:
                if preflight_mode == "separate":
                    probe_started = time.perf_counter()
                    probe_timeout = max(5, min(15, timeout // 4 if timeout else 5))
                    probe_ok, probe_log = run_prism_probe(stage, timeout=probe_timeout)
                    preflight_seconds = time.perf_counter() - probe_started
                    if not probe_ok:
                        log_text = (
                            "Prism health probe failed before export. Prism may need restart "
                            f"or a blocking dialog may need attention.\n{probe_log}"
                        )
                        return (False, f"{log_text}\nStaging preserved for diagnosis: {stage}", final_svg)

                staged_project = stage / project.name
                shutil.copy2(project, staged_project)
                staged_script = create_export_script(
                    staged_project,
                    stage,
                    basename,
                    keep_prism_warm=keep_prism_warm,
                    graph_title=graph_title,
                    x_axis_title=x_axis_title,
                    y_axis_title=y_axis_title,
                    y_axis_limits=y_axis_limits,
                    x_axis_limits=x_axis_limits,
                    palette_name=palette_name,
                    graph_index=graph_index,
                    inline_health_check=preflight_mode == "inline",
                )
                native_started = time.perf_counter()
                ok, log_text = run_prism_script(staged_script, timeout=timeout)
                native_seconds = time.perf_counter() - native_started

                staged_svg = stage / f"{basename}.svg"
                staged_log = staged_script.with_suffix(".log")
                if staged_svg.exists():
                    shutil.copy2(staged_svg, final_svg)
                    try:
                        titles = dict(
                            graph_title=graph_title,
                            x_axis_title=x_axis_title,
                            y_axis_title=y_axis_title,
                        )
                        visible = svg_render.titles_visible(final_svg, **titles)
                        missing = {
                            key: value
                            for key, value in titles.items()
                            if value and not visible[key]
                        }
                        if missing and title_mode == "native":
                            raise ValueError(
                                "Native Prism titles are hidden: " + ", ".join(missing)
                            )
                        if not missing:
                            log_text = (
                                f"{log_text.rstrip()}\nNative SVG titles verified."
                            )
                        if add_svg_title_overlay(
                            final_svg,
                            **missing,
                        ):
                            log_text = (
                                f"{log_text.rstrip()}\nSVG title overlay applied."
                            )
                    except Exception as exc:
                        ok = False
                        log_text = f"{log_text.rstrip()}\nUnable to add SVG title overlay: {exc}"
                if staged_log.exists():
                    shutil.copy2(staged_log, final_log)
                export_ok = ok and staged_svg.exists() and final_svg.exists()
                if export_ok:
                    shutil.copy2(staged_project, project)
                    if clean_metadata and project.suffix.lower() == ".pzfx":
                        sanitize_project_metadata(project)
            finally:
                timing = {"schema": 1, "preflight_mode": preflight_mode,
                          "preflight_seconds": preflight_seconds,
                          "native_seconds": native_seconds,
                          "total_seconds": time.perf_counter() - started,
                          "export_success": export_ok,
                          "note": "Native time includes transport, dispatch and result synchronization; not render time alone."}
                try:
                    (outdir / f"{basename}.timing.json").write_text(json.dumps(timing, indent=2), encoding="utf-8")
                except OSError:
                    pass  # Profiling must not mask a failure or erase its journal.
                if export_ok:
                    shutil.rmtree(stage, ignore_errors=True)
    except TimeoutError as exc:
        return False, str(exc), final_svg
    if not export_ok:
        detail = f"Staging preserved for diagnosis: {stage}"
        log_text = f"{log_text.rstrip()}\n{detail}" if log_text.strip() else detail
    return export_ok, log_text, final_svg


def add_svg_title_overlay(
    svg: Path,
    *,
    graph_title: str | None = None,
    x_axis_title: str | None = None,
    y_axis_title: str | None = None,
) -> bool:
    if not any((graph_title, x_axis_title, y_axis_title)):
        return False
    text = svg.read_text(encoding="utf-8")
    opening = re.search(r"<svg\b[^>]*>", text)
    closing_index = text.rfind("</svg>")
    if opening is None or closing_index < opening.end():
        raise ValueError(f"Invalid SVG root in {svg}")

    width_match = re.search(r'\bwidth="([0-9.]+)pt"', opening.group(0))
    height_match = re.search(r'\bheight="([0-9.]+)pt"', opening.group(0))
    viewbox_match = re.search(
        r'\bviewBox="([0-9.+-]+)\s+([0-9.+-]+)\s+([0-9.]+)\s+([0-9.]+)"',
        opening.group(0),
    )
    if viewbox_match:
        width = float(viewbox_match.group(3))
        height = float(viewbox_match.group(4))
    elif width_match and height_match:
        width = float(width_match.group(1))
        height = float(height_match.group(1))
    else:
        raise ValueError(f"SVG has no numeric point size/viewBox: {svg}")

    def estimated_width(
        value: str | None, font_size: float, *, bold: bool = False
    ) -> float:
        if not value:
            return 0.0
        em = 0.0
        for character in value:
            if unicodedata.east_asian_width(character) in {"W", "F", "A"}:
                em += 1.0
            elif character.isspace():
                em += 0.33
            elif character in "ilI.,:;'|!":
                em += 0.3
            elif character == "@":
                em += 1.05
            elif character == "%":
                em += 0.92
            elif character == "&":
                em += 0.68
            elif character in "mM":
                em += 0.83
            elif character == "w":
                em += 0.72
            elif character == "W":
                em += 0.94
            elif character in "GOQ":
                em += 0.78
            elif character.isupper():
                em += 0.72
            else:
                em += 0.56
        return em * font_size * (1.06 if bold else 1.0)

    left = 36 if y_axis_title else 0
    top = 28 if graph_title else 0
    bottom = 30 if x_axis_title else 0
    base_width = width + left
    new_width = max(
        base_width,
        estimated_width(graph_title, 12, bold=True) + 32,
        estimated_width(x_axis_title, 10) + 32,
    )
    base_height = height + top + bottom
    new_height = max(base_height, estimated_width(y_axis_title, 10) + 32)
    graph_x = left + (new_width - base_width) / 2
    graph_y = top + (new_height - base_height) / 2

    root_tag = opening.group(0)
    root_tag = re.sub(r'\bwidth="[^"]+"', f'width="{new_width:g}pt"', root_tag)
    root_tag = re.sub(r'\bheight="[^"]+"', f'height="{new_height:g}pt"', root_tag)
    if re.search(r'\bviewBox="[^"]+"', root_tag):
        root_tag = re.sub(
            r'\bviewBox="[^"]+"',
            f'viewBox="0 0 {new_width:g} {new_height:g}"',
            root_tag,
        )
    else:
        root_tag = root_tag[:-1] + f' viewBox="0 0 {new_width:g} {new_height:g}">'

    body = text[opening.end() : closing_index]
    labels: list[str] = []
    if graph_title:
        labels.append(
            f'<text x="{new_width / 2:g}" y="18" text-anchor="middle" '
            f'font-family="Arial, Helvetica, sans-serif" font-size="12" font-weight="700" '
            f'fill="#000000">{html.escape(graph_title)}</text>'
        )
    if x_axis_title:
        labels.append(
            f'<text x="{new_width / 2:g}" y="{graph_y + height + 24:g}" text-anchor="middle" '
            f'font-family="Arial, Helvetica, sans-serif" font-size="10" '
            f'fill="#000000">{html.escape(x_axis_title)}</text>'
        )
    if y_axis_title:
        labels.append(
            f'<text transform="translate(14 {new_height / 2:g}) rotate(-90)" '
            f'text-anchor="middle" font-family="Arial, Helvetica, sans-serif" font-size="10" '
            f'fill="#000000">{html.escape(y_axis_title)}</text>'
        )
    rewritten = (
        text[: opening.start()]
        + root_tag
        + f'<rect x="0" y="0" width="{new_width:g}" height="{new_height:g}" fill="#ffffff"/>'
        + f'<g transform="translate({graph_x:g} {graph_y:g})">'
        + body
        + "</g>"
        + "".join(labels)
        + "</svg>"
    )
    svg.write_text(rewritten, encoding="utf-8")
    return True


def pptx_xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def rasterize_svg_for_pptx(svg: Path, png: Path) -> None:
    svg_render.render(svg, png)


def fallback_preview_svg(
    data: Path,
    out: Path,
    title: str,
    *,
    first_row_is_data: bool | None = None,
) -> None:
    rows = read_table(data)
    headers, body = split_header(rows, first_row_is_data)
    out.parent.mkdir(parents=True, exist_ok=True)
    max_rows = 12
    x = 70
    y = 110
    row_height = 32
    col_width = max(110, min(220, 820 // max(1, len(headers))))
    header_cells = "\n".join(
        f'<text x="{x + idx * col_width}" y="{y}" font-size="18" font-weight="700">'
        f"{html.escape(header or f'Column {idx + 1}')}</text>"
        for idx, header in enumerate(headers[:6])
    )
    body_lines = []
    for row_index, row in enumerate(body[:max_rows], start=1):
        yy = y + row_index * row_height
        for idx, _ in enumerate(headers[:6]):
            value = row[idx] if idx < len(row) else ""
            body_lines.append(
                f'<text x="{x + idx * col_width}" y="{yy}" font-size="17">'
                f"{html.escape(value)}</text>"
            )
    note = "Fallback preview: Prism export did not complete; linked .pzfx remains editable."
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="600" viewBox="0 0 1000 600">
  <rect width="1000" height="600" fill="#ffffff"/>
  <rect x="36" y="34" width="928" height="532" rx="12" fill="#f8fafc" stroke="#cbd5e1" stroke-width="2"/>
  <text x="70" y="76" font-family="Arial, Helvetica, sans-serif" font-size="28" font-weight="700" fill="#111827">{html.escape(title)}</text>
  <text x="70" y="545" font-family="Arial, Helvetica, sans-serif" font-size="15" fill="#64748b">{html.escape(note)}</text>
  <g font-family="Arial, Helvetica, sans-serif" fill="#111827">
    {header_cells}
    <line x1="70" y1="124" x2="930" y2="124" stroke="#94a3b8" stroke-width="1"/>
    {"".join(body_lines)}
  </g>
</svg>
"""
    out.write_text(svg, encoding="utf-8")


def raster_image_size(path):
    """Read PNG/JPEG dimensions without adding a server-side imaging dependency."""
    data = Path(path).read_bytes()
    if (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        and len(data) >= 24
        and data[12:16] == b"IHDR"
    ):
        width, height = struct.unpack(">II", data[16:24])
        if width and height:
            return width, height
    if data.startswith(b"\xff\xd8"):
        i = 2
        while i + 3 < len(data):
            if data[i] != 255:
                break
            while i < len(data) and data[i] == 255:
                i += 1
            if i >= len(data):
                break
            marker = data[i]
            i += 1
            if marker in {0xD9, 0xDA} or i + 2 > len(data):
                break
            if marker in {0xD8, 0x01, *range(0xD0, 0xD8)}:
                continue
            size = struct.unpack(">H", data[i : i + 2])[0]
            if size < 2 or i + size > len(data):
                break
            if (
                marker
                in {
                    0xC0,
                    0xC1,
                    0xC2,
                    0xC3,
                    0xC5,
                    0xC6,
                    0xC7,
                    0xC9,
                    0xCA,
                    0xCB,
                    0xCD,
                    0xCE,
                    0xCF,
                }
                and size >= 8
            ):
                height, width = struct.unpack(">HH", data[i + 3 : i + 7])
                if width and height:
                    return width, height
            i += size
    raise ValueError("Cannot read valid PNG/JPEG dimensions for PPTX")


def minimal_pptx(image: Path, pzfx: Path, out: Path, title: str) -> None:
    if not image.exists():
        raise FileNotFoundError(f"Image not found for PPTX: {image}")
    out.parent.mkdir(parents=True, exist_ok=True)
    image_ext = image.suffix.lower().lstrip(".")
    if image_ext not in {"png", "jpg", "jpeg", "svg"}:
        raise ValueError("PPTX image must be png, jpg, jpeg, or svg")
    rel_path = pzfx.resolve().as_uri()
    title_xml = pptx_xml_escape(title)
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    with tempfile.TemporaryDirectory(prefix="prism_pptx_") as tmp:
        tmp_path = Path(tmp)
        native_title_present = False
        if image_ext == "svg":
            svg_text = image.read_text(encoding="utf-8-sig")
            if re.search(r"<!ENTITY\b|<!DOCTYPE[^>]*\[", svg_text, re.I | re.S):
                raise ValueError(
                    "SVG DTD/entity definitions are not supported for Office embedding"
                )
            # Office's XML reader prohibits DTDs. Keep the original native SVG
            # untouched and omit only its unused declaration in the PPT copy.
            embedded_svg = re.sub(
                r"<!DOCTYPE\s+svg\b[^>]*>\s*", "", svg_text, flags=re.I | re.S
            )
            ET.fromstring(embedded_svg)
            native_title_present = (
                bool(title)
                and svg_render.titles_visible(image, graph_title=title)["graph_title"]
            )
            fallback_image = tmp_path / "image1.png"
            rasterize_svg_for_pptx(image, fallback_image)
            fallback_name = "image1.png"
            fallback_content_type = "image/png"
            svg_name = "image1.svg"
            svg_extension = (
                '<a:extLst><a:ext uri="{96DAC541-7B7A-43D3-8B79-37D633B846F1}">'
                f'<asvg:svgBlip xmlns:asvg="http://schemas.microsoft.com/office/drawing/2016/SVG/main" '
                f'xmlns:r="{PPTX_NS["r"]}" r:embed="rId3"/>'
                "</a:ext></a:extLst>"
            )
            hyperlink_id = "rId4"
            slide_image_relationships = (
                '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image1.png"/>'
                '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image1.svg"/>'
            )
        else:
            fallback_image = image
            fallback_name = f"image1.{image_ext}"
            fallback_content_type = "image/png" if image_ext == "png" else "image/jpeg"
            svg_name = None
            svg_extension = ""
            hyperlink_id = "rId3"
            slide_image_relationships = f'<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/{fallback_name}"/>'

        pixel_width, pixel_height = raster_image_size(fallback_image)
        box_x, box_y = (457200, 457200) if native_title_present else (914400, 914400)
        box_width, box_height = (
            (8229600, 4229100) if native_title_present else (7315200, 3771900)
        )
        scale = min(box_width / pixel_width, box_height / pixel_height)
        figure_width, figure_height = round(pixel_width * scale), round(
            pixel_height * scale
        )
        if min(figure_width, figure_height) < 1:
            raise ValueError("Image aspect ratio is too extreme for a figure slide")
        figure_x = box_x + (box_width - figure_width) // 2
        figure_y = box_y + (box_height - figure_height) // 2
        title_shape = (
            ""
            if native_title_present
            else f"""<p:sp>
      <p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
      <p:spPr><a:xfrm><a:off x="457200" y="228600"/><a:ext cx="8229600" cy="457200"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/><a:ln><a:noFill/></a:ln></p:spPr>
      <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="en-US" sz="2400"/><a:t>{title_xml}</a:t></a:r><a:endParaRPr lang="en-US" sz="2400"/></a:p></p:txBody>
    </p:sp>"""
        )

        slide = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="{PPTX_NS['a']}" xmlns:r="{PPTX_NS['r']}" xmlns:p="{PPTX_NS['p']}">
  <p:cSld name="Prism Figure"><p:spTree>
    <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
    <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
    {title_shape}
    <p:pic>
      <p:nvPicPr><p:cNvPr id="3" name="Prism figure" descr="{title_xml}"><a:hlinkClick r:id="{hyperlink_id}"/></p:cNvPr><p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr>
      <p:blipFill><a:blip r:embed="rId2">{svg_extension}</a:blip><a:stretch><a:fillRect/></a:stretch></p:blipFill>
      <p:spPr><a:xfrm><a:off x="{figure_x}" y="{figure_y}"/><a:ext cx="{figure_width}" cy="{figure_height}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
    </p:pic>
  </p:spTree></p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>"""

        files = {
            "[Content_Types].xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{PPTX_NS['ct']}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Default Extension="jpg" ContentType="image/jpeg"/>
  <Default Extension="jpeg" ContentType="image/jpeg"/>
  <Default Extension="svg" ContentType="image/svg+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
  <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
  <Override PartName="/ppt/presProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presProps+xml"/>
  <Override PartName="/ppt/tableStyles.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.tableStyles+xml"/>
  <Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
  <Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
  <Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
  <Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
</Types>""",
            "_rels/.rels": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{PPTX_NS['rel']}">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>""",
            "docProps/core.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:title>{title_xml}</dc:title><dc:creator>Codex Prism Figure Bridge</dc:creator><cp:lastModifiedBy>Codex Prism Figure Bridge</cp:lastModifiedBy><dcterms:created xsi:type="dcterms:W3CDTF">{created}</dcterms:created><dcterms:modified xsi:type="dcterms:W3CDTF">{created}</dcterms:modified></cp:coreProperties>""",
            "docProps/app.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>Codex Prism Figure Bridge</Application><PresentationFormat>Widescreen</PresentationFormat><Slides>1</Slides><Notes>0</Notes><HiddenSlides>0</HiddenSlides><MMClips>0</MMClips><ScaleCrop>false</ScaleCrop><Company></Company><LinksUpToDate>false</LinksUpToDate><SharedDoc>false</SharedDoc><HyperlinksChanged>false</HyperlinksChanged><AppVersion>1.0</AppVersion></Properties>""",
            "ppt/presentation.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="{PPTX_NS['a']}" xmlns:r="{PPTX_NS['r']}" xmlns:p="{PPTX_NS['p']}">
  <p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>
  <p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>
  <p:sldSz cx="9144000" cy="5143500" type="screen16x9"/><p:notesSz cx="6858000" cy="9144000"/>
  <p:defaultTextStyle><a:defPPr/><a:lvl1pPr marL="0" algn="l" defTabSz="914400"><a:defRPr lang="en-US"/></a:lvl1pPr></p:defaultTextStyle>
</p:presentation>""",
            "ppt/_rels/presentation.xml.rels": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{PPTX_NS['rel']}">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/presProps" Target="presProps.xml"/>
  <Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/>
  <Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/tableStyles" Target="tableStyles.xml"/>
</Relationships>""",
            "ppt/presProps.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:presentationPr xmlns:a="{PPTX_NS['a']}" xmlns:r="{PPTX_NS['r']}" xmlns:p="{PPTX_NS['p']}"/>""",
            "ppt/tableStyles.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?><a:tblStyleLst xmlns:a="{PPTX_NS['a']}" def="{{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}}"/>""",
            "ppt/theme/theme1.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="{PPTX_NS['a']}" name="Prism Figure Theme"><a:themeElements>
<a:clrScheme name="Prism"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1><a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="1F1F1F"/></a:dk2><a:lt2><a:srgbClr val="F2F2F2"/></a:lt2><a:accent1><a:srgbClr val="1F5AA6"/></a:accent1><a:accent2><a:srgbClr val="70AD47"/></a:accent2><a:accent3><a:srgbClr val="ED7D31"/></a:accent3><a:accent4><a:srgbClr val="A5A5A5"/></a:accent4><a:accent5><a:srgbClr val="FFC000"/></a:accent5><a:accent6><a:srgbClr val="5B9BD5"/></a:accent6><a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink></a:clrScheme>
<a:fontScheme name="Prism"><a:majorFont><a:latin typeface="Arial"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="Arial"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme>
<a:fmtScheme name="Prism"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"><a:tint val="50000"/><a:satMod val="300000"/></a:schemeClr></a:solidFill><a:solidFill><a:schemeClr val="phClr"><a:shade val="50000"/></a:schemeClr></a:solidFill></a:fillStyleLst><a:lnStyleLst><a:ln w="6350"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln><a:ln w="12700"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln><a:ln w="19050"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln></a:lnStyleLst><a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst><a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"><a:tint val="95000"/><a:satMod val="170000"/></a:schemeClr></a:solidFill><a:solidFill><a:schemeClr val="phClr"><a:shade val="20000"/></a:schemeClr></a:solidFill></a:bgFillStyleLst></a:fmtScheme>
</a:themeElements><a:objectDefaults/><a:extraClrSchemeLst/></a:theme>""",
            "ppt/slideMasters/slideMaster1.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="{PPTX_NS['a']}" xmlns:r="{PPTX_NS['r']}" xmlns:p="{PPTX_NS['p']}"><p:cSld name="Prism Master"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld><p:clrMap accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" bg1="lt1" bg2="lt2" folHlink="folHlink" hlink="hlink" tx1="dk1" tx2="dk2"/><p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst><p:txStyles><p:titleStyle><a:lvl1pPr algn="l"><a:defRPr sz="2400" b="1"/></a:lvl1pPr></p:titleStyle><p:bodyStyle><a:lvl1pPr marL="342900" indent="-342900"><a:defRPr sz="1800"/></a:lvl1pPr></p:bodyStyle><p:otherStyle><a:defPPr/><a:lvl1pPr marL="0"><a:defRPr sz="1800"/></a:lvl1pPr></p:otherStyle></p:txStyles></p:sldMaster>""",
            "ppt/slideMasters/_rels/slideMaster1.xml.rels": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{PPTX_NS['rel']}"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/></Relationships>""",
            "ppt/slideLayouts/slideLayout1.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldLayout xmlns:a="{PPTX_NS['a']}" xmlns:r="{PPTX_NS['r']}" xmlns:p="{PPTX_NS['p']}" type="blank" preserve="1"><p:cSld name="Blank"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>""",
            "ppt/slideLayouts/_rels/slideLayout1.xml.rels": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{PPTX_NS['rel']}"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>""",
            "ppt/slides/slide1.xml": slide,
            "ppt/slides/_rels/slide1.xml.rels": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{PPTX_NS['rel']}"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>{slide_image_relationships}<Relationship Id="{hyperlink_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="{pptx_xml_escape(rel_path)}" TargetMode="External"/></Relationships>""",
        }
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, text in files.items():
                archive.writestr(name, text)
            archive.write(fallback_image, f"ppt/media/{fallback_name}")
            if svg_name:
                archive.writestr(f"ppt/media/{svg_name}", embedded_svg.encode("utf-8"))


def list_templates() -> None:
    if SKILL_TEMPLATE_INDEX.exists():
        curated_path = SKILL_TEMPLATE_ROOT / "curated_templates.json"
        whitelist: set[str] = set()
        if curated_path.exists():
            whitelist = set(
                json.loads(curated_path.read_text(encoding="utf-8")).get(
                    "whitelist_automation", []
                )
            )
        index = json.loads(SKILL_TEMPLATE_INDEX.read_text(encoding="utf-8"))
        for entry in sorted(
            index.get("templates", []), key=lambda item: item.get("alias", "")
        ):
            if entry.get("alias") not in whitelist:
                continue
            path = SKILL_TEMPLATE_ROOT / entry["relative_path"]
            exists = "ok" if path.exists() else "missing"
            print(
                f"{entry['alias']:32} {exists:7} {entry.get('kind', 'unknown'):10} "
                f"{entry.get('table_type') or '-':8} {path.name}"
            )
        return

    for alias, rel in sorted(TEMPLATE_ALIASES.items()):
        path = DEFAULT_TEMPLATE_ROOT / rel
        kind = "zip-bundle" if path.exists() and is_zip(path) else "legacy-xml"
        exists = "ok" if path.exists() else "missing"
        print(f"{alias:22} {exists:7} {kind:10} {path}")


def environment() -> None:
    target=execution_target()
    print(json.dumps({'platform':platform.system(),'execution_target':target,'native_execution_ready':target['ready'],
                     'prism_processes':prism_process_inventory(),'export_format':'svg','ppt_editable_ole':'Windows native OLE'},ensure_ascii=False))
    return
    print(f"platform: {platform.platform()}")
    print(
        f"prism_app: {DEFAULT_PRISM_APP if DEFAULT_PRISM_APP.exists() else 'not found'}"
    )
    print(f"export_format: svg")
    try:
        with (DEFAULT_PRISM_APP/'Contents/Info.plist').open('rb')as f:
            out=plistlib.load(f).get('CFBundleShortVersionString','unknown')
    except Exception as exc:
        out = f"unavailable ({exc})"
    print(f"prism_version: {out}")
    ppt = Path("/Applications/Microsoft PowerPoint.app")
    print(f"powerpoint_app: {ppt if ppt.exists() else 'not found'}")
    prefs = Path.home() / "Library/Preferences/com.GraphPad.Prism.plist"
    if prefs.exists():
        with prefs.open("rb") as f:
            data = plistlib.load(f)
        print(f"prism_startup_dialog_seen: {'NSWindow Frame StartupDialog 11' in data}")
    print("automation: open .pzc via Prism (no GUI click automation)")
    print(
        "ppt_editable_ole: unsupported on macOS; supported only by Windows OLE/ActiveX"
    )


def build(args: argparse.Namespace) -> None:
    if not args.hint.strip():
        raise ValueError(
            "A non-empty figure hint is required. Run prism_match_template first, "
            "then pass the intended figure type with --hint."
        )
    outdir = Path(args.outdir).expanduser().resolve()
    requested_template = args.template
    if not requested_template:
        match = template_matcher.match_templates(
            SKILL_ROOT,
            Path(args.data).expanduser(),
            args.hint,
            first_row_is_data=args.first_row_is_data,
        )
        requested_template = match.get("recommended")
        if not requested_template:
            raise ValueError(
                "Blocked figure type: "
                + (match.get("blocked_reason") or "No compatible template")
            )
    template = resolve_template(requested_template)
    entry = indexed_template_entries().get(template, {})
    if args.graph_index < 1 or (
        entry.get("graph_count") and args.graph_index > entry["graph_count"]
    ):
        raise ValueError("graph_index is outside this template's graph range")
    basename = args.name or Path(args.data).stem
    project = outdir / f"{basename}.pzfx"

    patch_mode = build_project(
        template,
        Path(args.data).expanduser(),
        project,
        args.title,
        hint=args.hint,
        first_row_is_data=args.first_row_is_data,
    )
    default_x_title, default_y_title = default_axis_titles(
        Path(args.data).expanduser(), args.first_row_is_data, args.hint
    )
    x_axis_title = (
        args.x_axis_title if args.x_axis_title is not None else default_x_title
    )
    y_axis_title = (
        args.y_axis_title if args.y_axis_title is not None else default_y_title
    )
    headers, _ = split_header(
        read_table(Path(args.data).expanduser()), args.first_row_is_data
    )
    if default_x_title is not None:
        headers = headers[1:]
    colors = json.loads(args.group_colors) if args.group_colors else None
    if colors is not None:
        alias = indexed_template_entries().get(template, {}).get("alias", template.stem)
        palette = template_styles.apply_colors(project, alias, headers, colors)
    else:
        palette = native_palette.prepare_palette(headers, args.palette)
    style = dict(palette_name=palette["name"], graph_index=args.graph_index)
    if not args.keep_template_axis and entry.get("alias") == "watercolor-xy-lines":
        style["x_axis_limits"] = numeric_x_limits(
            read_table(Path(args.data).expanduser()), args.first_row_is_data
        )
    y_axis_limits = (
        None
        if args.keep_template_axis
        else numeric_axis_limits(
            read_table(Path(args.data).expanduser()),
            args.first_row_is_data,
            has_x_column=default_x_title is not None,
        )
    )
    if y_axis_limits and template.stem == "watercolor-bars-sem":
        lo, hi, step = y_axis_limits
        y_axis_limits = (min(0, lo), max(0, hi), step)
    script = create_export_script(
        project,
        outdir,
        basename,
        keep_prism_warm=args.keep_prism_warm and not args.close_prism,
        graph_title=args.title or basename,
        x_axis_title=x_axis_title,
        y_axis_title=y_axis_title,
        y_axis_limits=y_axis_limits,
        **style,
    )

    print(f"project: {project}")
    print(f"export_script: {script}")
    print(f"patch_mode: {patch_mode}")

    exported_image = None
    prism_failed = False
    should_run_prism = args.run_prism or (bool(args.pptx) and not args.image)
    if should_run_prism:
        ok, message, staged_svg = run_prism_export_staged(
            project,
            outdir,
            basename,
            timeout=args.timeout,
            keep_prism_warm=args.keep_prism_warm and not args.close_prism,
            graph_title=args.title or basename,
            x_axis_title=x_axis_title,
            y_axis_title=y_axis_title,
            y_axis_limits=y_axis_limits,
            **style,
            title_mode=args.title_mode,
        )
        print(f"prism_run: {'ok' if ok else 'failed'}")
        prism_failed = not ok
        if not ok:
            print(message.strip())
        if ok and staged_svg.exists():
            exported_image = staged_svg

    if args.image:
        exported_image = Path(args.image).expanduser().resolve()
    if args.pptx:
        pptx_path = Path(args.pptx).expanduser().resolve()
        if not exported_image or prism_failed or exported_image.name.endswith('_fallback.svg'):
            raise ValueError('Windows editable PPT requires a verified native Prism export')
        windows_prism_ppt.package(project,pptx_path,args.title or basename)
        print(f"pptx: {pptx_path}")
    if prism_failed and not args.pptx:
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("env", help="Check local Prism/PowerPoint bridge capabilities")
    sub.add_parser('execution-status',help='Read the native operation journal without querying or launching Prism')
    recovery=sub.add_parser('recover-execution',help='Resolve one exact uncertain operation without quitting Prism or replaying it')
    recovery.add_argument('--operation-id',required=True)
    sub.add_parser(
        "list-templates", help="List automation-whitelisted template aliases"
    )

    p_build = sub.add_parser("build", help="Create a Prism project and optional PPTX")
    p_build.add_argument("--data", required=True, help="CSV/TSV raw data")
    p_build.add_argument(
        "--template", help="Template alias or path; omitted: match data and --hint"
    )
    p_build.add_argument(
        "--outdir", default="outputs/prism_bridge_run", help="Output folder"
    )
    p_build.add_argument("--name", help="Base output name")
    p_build.add_argument("--title", help="Graph/table title")
    p_build.add_argument("--x-axis-title", help="Override the Prism X-axis title")
    p_build.add_argument("--y-axis-title", help="Override the Prism Y-axis title")
    p_build.add_argument(
        "--palette", help="Native scheme name, e.g. Pastels, or inherit"
    )
    p_build.add_argument(
        "--group-colors", help='JSON group-name to HEX map, e.g. {"Control":"#78B4A4"}'
    )
    p_build.add_argument("--graph-index", type=int, default=1)
    p_build.add_argument(
        "--title-mode", choices=("auto", "native", "overlay"), default="auto"
    )
    p_build.add_argument(
        "--keep-template-axis",
        action="store_true",
        help="Keep the template's original Y-axis range instead of deriving one from the data",
    )
    p_build.add_argument(
        "--hint", default="", help="Figure/data hint used for Column vs XY profiling"
    )
    first_row = p_build.add_mutually_exclusive_group()
    first_row.add_argument(
        "--first-row-is-data",
        dest="first_row_is_data",
        action="store_true",
        help="Treat an all-numeric first row as observations and synthesize Y1..Yn headers",
    )
    first_row.add_argument(
        "--first-row-is-header",
        dest="first_row_is_data",
        action="store_false",
        help="Treat an all-numeric first row as column headers (for example years or doses)",
    )
    p_build.set_defaults(first_row_is_data=None)
    p_build.add_argument(
        "--run-prism", action="store_true", help="Ask Prism to export SVG"
    )
    p_build.add_argument(
        "--close-prism", action="store_true", help="Close Prism project after export"
    )
    p_build.add_argument(
        "--keep-prism-warm",
        action="store_true",
        help="Leave Prism project open after export",
    )
    p_build.add_argument(
        "--timeout", type=int, default=120, help="Prism script timeout seconds"
    )
    p_build.add_argument("--image", help="Existing SVG/PNG/JPG to place in PPTX")
    p_build.add_argument("--pptx", help="Write a one-slide PPTX")

    args = parser.parse_args(argv)
    if args.cmd == "env":
        environment()
    elif args.cmd=='execution-status':
        print(json.dumps(execution_status(),ensure_ascii=False))
    elif args.cmd=='recover-execution':
        try:print(json.dumps(recover_execution(args.operation_id),ensure_ascii=False))
        except (ValueError,RuntimeError,TimeoutError)as exc:
            print(str(exc),file=sys.stderr);return 2
    elif args.cmd == "list-templates":
        list_templates()
    elif args.cmd == "build":
        try:
            build(args)
        except (ValueError, FileNotFoundError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
