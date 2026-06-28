#!/usr/bin/env python3
"""Score Prism templates against CSV/TSV data shape and optional text hints."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any


def read_table(path: Path) -> list[list[str]]:
    text = path.read_text(encoding="utf-8-sig")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel_tab if "\t" in sample else csv.excel
    rows = [[cell.strip() for cell in row] for row in csv.reader(text.splitlines(), dialect)]
    return [row for row in rows if any(cell.strip() for cell in row)]


def split_header(rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    first = rows[0]
    numeric = 0
    for cell in first:
        try:
            float(cell)
            numeric += 1
        except ValueError:
            pass
    if numeric <= max(0, len(first) // 2 - 1):
        return first, rows[1:]
    return [f"Y{i + 1}" for i in range(len(first))], rows


def infer_data_profile(rows: list[list[str]]) -> dict[str, Any]:
    if not rows:
        return {"column_count": 0, "row_count": 0, "table_type": "Column", "has_x_column": False}
    headers, body = split_header(rows)
    column_count = len(headers)
    first_col_numeric = all(_is_number(row[0]) for row in body if row and row[0] != "")
    other_cols_numeric = column_count > 1 and all(
        _is_number(row[i])
        for row in body
        for i in range(1, min(column_count, len(row)))
        if row[i] != ""
    )
    has_x_column = column_count >= 2 and first_col_numeric and other_cols_numeric
    table_type = "XY" if has_x_column else "Column"
    return {
        "column_count": column_count,
        "row_count": len(body),
        "table_type": table_type,
        "has_x_column": has_x_column,
        "headers": headers,
    }


def _is_number(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


def load_index(skill_root: Path) -> dict[str, Any]:
    path = skill_root / "assets/templates/template_index.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_curated(skill_root: Path) -> dict[str, Any]:
    path = skill_root / "assets/templates/curated_templates.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _entry_by_alias(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["alias"]: entry for entry in index.get("templates", [])}


def score_template(entry: dict[str, Any], profile: dict[str, Any], hint_text: str, whitelist: set[str]) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    if entry.get("kind") != "xml":
        return -1000, ["binary or zip bundle; cannot auto-patch data"]
    if entry.get("collection") != "portfolio":
        score -= 50
        reasons.append("downloaded template; lower priority than portfolio")
    else:
        score += 30
        reasons.append("portfolio template")
    if entry.get("validation") == "ok":
        score += 20
        reasons.append("validated in Prism")
    if entry["alias"] in whitelist:
        score += 40
        reasons.append("automation whitelist")

    entry_table = entry.get("table_type")
    if entry_table and profile["table_type"] and entry_table == profile["table_type"]:
        score += 35
        reasons.append(f"table type matches ({entry_table})")
    elif entry_table and profile["table_type"] and entry_table != profile["table_type"]:
        score -= 40
        reasons.append(f"table type mismatch (data={profile['table_type']}, template={entry_table})")

    entry_has_x = entry.get("has_x_column")
    if entry_has_x is not None and entry_has_x == profile["has_x_column"]:
        score += 25
        reasons.append("X column expectation matches data")
    elif entry_has_x is not None and entry_has_x != profile["has_x_column"]:
        score -= 30
        reasons.append("X column expectation mismatches data")

    y_count = entry.get("y_column_count")
    if y_count is not None and profile["column_count"]:
        expected_y = profile["column_count"] - (1 if profile["has_x_column"] else 0)
        delta = abs(y_count - expected_y)
        if delta == 0:
            score += 20
            reasons.append(f"y column count matches ({y_count})")
        elif delta <= 2:
            score += 10
            reasons.append(f"y column count close ({y_count} vs {expected_y})")
        else:
            score -= min(25, delta * 5)
            reasons.append(f"y column count differs ({y_count} vs {expected_y})")

    haystack = " ".join(
        [
            hint_text.lower(),
            entry.get("name", "").lower(),
            entry.get("alias", "").lower(),
            entry.get("first_title", "").lower(),
        ]
    )
    for token in re.findall(r"[a-z0-9]+", hint_text.lower()):
        if len(token) < 3:
            continue
        if token in haystack:
            score += 5

    return score, reasons


def curated_matches(hint_text: str, curated: dict[str, Any], by_alias: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    hint = hint_text.lower()
    hits: list[tuple[int, dict[str, Any], str]] = []
    for figure in curated.get("figure_types", []):
        keywords = figure.get("keywords", [])
        keyword_hits = sum(1 for kw in keywords if kw.lower() in hint)
        if keyword_hits == 0:
            continue
        alias = figure["template"]
        entry = by_alias.get(alias)
        if entry is None:
            continue
        hits.append((keyword_hits * 10, entry, figure["id"]))
    hits.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            "alias": entry["alias"],
            "name": entry.get("name"),
            "score": score,
            "source": "curated",
            "figure_type": figure_id,
            "reasons": [f"curated keyword match ({figure_id})"],
        }
        for score, entry, figure_id in hits[:5]
    ]


def match_templates(
    skill_root: Path,
    data_path: Path,
    hint: str = "",
    limit: int = 5,
    automation_only: bool = True,
) -> dict[str, Any]:
    rows = read_table(data_path)
    profile = infer_data_profile(rows)
    index = load_index(skill_root)
    curated = load_curated(skill_root)
    by_alias = _entry_by_alias(index)
    whitelist = set(curated.get("whitelist_automation", []))
    hint_text = hint.strip()

    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    for hit in curated_matches(hint_text, curated, by_alias):
        if hit["alias"] in seen:
            continue
        seen.add(hit["alias"])
        results.append(hit)

    candidates = index.get("templates", [])
    if automation_only:
        candidates = [entry for entry in candidates if entry.get("alias") in whitelist]

    scored: list[tuple[int, dict[str, Any], list[str]]] = []
    for entry in candidates:
        score, reasons = score_template(entry, profile, hint_text, whitelist)
        if score < 0:
            continue
        scored.append((score, entry, reasons))
    scored.sort(key=lambda item: item[0], reverse=True)

    for score, entry, reasons in scored:
        if entry["alias"] in seen:
            continue
        seen.add(entry["alias"])
        results.append(
            {
                "alias": entry["alias"],
                "name": entry.get("name"),
                "score": score,
                "source": "index",
                "relative_path": entry.get("relative_path"),
                "table_type": entry.get("table_type"),
                "has_x_column": entry.get("has_x_column"),
                "y_column_count": entry.get("y_column_count"),
                "reasons": reasons,
            }
        )
        if len(results) >= limit:
            break

    return {
        "data_profile": profile,
        "hint": hint_text,
        "automation_only": automation_only,
        "matches": results[:limit],
        "recommended": results[0]["alias"] if results else None,
    }
