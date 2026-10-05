#!/usr/bin/env python3
"""Score Prism templates against CSV/TSV data shape and optional text hints."""

from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path
from typing import Any

from template_identity import requested_template_number, resolve_identity


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
    return [row for row in rows if any(cell.strip() for cell in row)]


def has_header_row(
    rows: list[list[str]], first_row_is_data: bool | None = None
) -> bool:
    if not rows:
        return False
    if first_row_is_data is not None:
        return not first_row_is_data
    if all(_is_number(cell) for cell in rows[0]):
        raise ValueError(
            "Ambiguous numeric first row: it could be numeric column headers "
            "(for example years or dose levels) or headerless observations. "
            "Set first_row_is_data=false to preserve it as headers, or "
            "first_row_is_data=true to treat it as data and synthesize Y1..Yn titles."
        )
    return True


def split_header(
    rows: list[list[str]], first_row_is_data: bool | None = None
) -> tuple[list[str], list[list[str]]]:
    first = rows[0]
    if has_header_row(rows, first_row_is_data):
        return first, rows[1:]
    return [f"Y{i + 1}" for i in range(len(first))], rows


def _normalized_words(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def normalize_hint_text(value: str) -> str:
    hint = value.lower()
    aliases = {
        "剂量反应": " dose response ",
        "量效": " dose response ",
        "浓度反应": " dose response ",
        "时间曲线": " time course line graph ",
        "时间过程": " time course line graph ",
        "动力学": " kinetics ",
        "折线": " line graph ",
        "曲线": " curve ",
        "柱状": " column bar ",
        "柱形": " column bar ",
        "条形": " bar ",
        "散点": " scatter dot ",
        "点图": " dot scatter ",
        "箱线": " box whisker ",
        "箱式": " box whisker ",
        "配对": " paired before after ",
        "前后": " before after ",
        "火山": " volcano differential expression ",
    }
    for source, replacement in aliases.items():
        hint = hint.replace(source, replacement)
    return hint


def _strictly_monotonic(values: list[float]) -> bool:
    if len(values) < 2 or len(set(values)) != len(values):
        return False
    return all(a < b for a, b in zip(values, values[1:])) or all(
        a > b for a, b in zip(values, values[1:])
    )


def _looks_like_x_header(header: str) -> bool:
    normalized = _normalized_words(header)
    tokens = normalized.split()
    compact = re.sub(r"[^a-z0-9]+", "", header.lower())
    exact = {
        "x",
        "time",
        "day",
        "days",
        "hour",
        "hours",
        "minute",
        "minutes",
        "second",
        "seconds",
        "dose",
        "concentration",
        "conc",
        "frequency",
        "distance",
        "temperature",
        "wavelength",
        "fold change",
        "log2 fold change",
    }
    first_tokens = {
        "x",
        "time",
        "day",
        "days",
        "hour",
        "hours",
        "minute",
        "minutes",
        "second",
        "seconds",
        "dose",
        "concentration",
        "conc",
        "frequency",
        "distance",
        "temperature",
        "wavelength",
    }
    compact_prefixes = (
        "logagonist",
        "logconcentration",
        "logdose",
        "log2fc",
        "logfc",
        "log2foldchange",
        "foldchange",
    )
    return (
        normalized in exact
        or bool(tokens and tokens[0] in first_tokens)
        or normalized.startswith("time ")
        or normalized.endswith(" time")
        or compact.startswith(compact_prefixes)
    )


def _hint_prefers_xy(hint_text: str) -> bool:
    hint = normalize_hint_text(hint_text)
    xy_phrases = (
        "xy",
        "time course",
        "kinetic",
        "dose response",
        "ic50",
        "ec50",
        "concentration",
        "line graph",
        "curve",
        "volcano",
        "spaghetti",
        "longitudinal",
    )
    column_phrases = (
        "column",
        "box",
        "whisker",
        "bar",
        "grouped",
        "before after",
        "paired",
    )
    return any(phrase in hint for phrase in xy_phrases) and not any(
        phrase in hint for phrase in column_phrases
    )


def _hint_requests_xy_scatter(hint_text: str) -> bool:
    raw = hint_text.lower()
    if any(
        phrase in raw for phrase in ("correlation", "相关性", "相关散点", "xy scatter")
    ):
        return True
    if any(
        phrase in raw
        for phrase in ("watercolor", "水彩", "箱线", "violin", "小提琴", "box", "bar")
    ):
        return False
    if any(phrase in raw for phrase in ("列散点图", "column scatter", "dot plot")):
        return False
    normalized = normalize_hint_text(hint_text)
    return any(
        phrase in raw or phrase in normalized
        for phrase in (
            "xy scatter",
            "scatter plot",
            "correlation",
            "相关性",
            "相关散点图",
            "散点图",
        )
    )


def infer_data_profile(
    rows: list[list[str]],
    hint_text: str = "",
    first_row_is_data: bool | None = None,
) -> dict[str, Any]:
    if not rows:
        return {
            "column_count": 0,
            "row_count": 0,
            "table_type": "Column",
            "has_x_column": False,
            "valid": False,
            "validation_reasons": ["No data rows found."],
        }
    header_present = has_header_row(rows, first_row_is_data)
    headers, body = split_header(rows, first_row_is_data)
    column_count = len(headers)
    first_col_numeric = all(_is_number(row[0]) for row in body if row and row[0] != "")
    other_cols_numeric = column_count > 1 and all(
        _is_number(row[i])
        for row in body
        for i in range(1, min(column_count, len(row)))
        if row[i] != ""
    )
    x_values = (
        [float(row[0]) for row in body if row and row[0] != ""]
        if first_col_numeric
        else []
    )
    numeric_xy_candidate = (
        column_count >= 2 and first_col_numeric and other_cols_numeric
    )
    x_header = header_present and bool(headers) and _looks_like_x_header(headers[0])
    monotonic_x = _strictly_monotonic(x_values)
    scatter_hint = header_present and _hint_requests_xy_scatter(hint_text)
    has_x_column = numeric_xy_candidate and (x_header or scatter_hint)
    table_type = "XY" if has_x_column else "Column"
    profile = {
        "column_count": column_count,
        "row_count": len(body),
        "table_type": table_type,
        "has_x_column": has_x_column,
        "headers": headers,
        "header_present": header_present,
        "first_row_is_data": not header_present,
        "x_evidence": {
            "header": x_header,
            "hint": _hint_prefers_xy(hint_text),
            "xy_scatter_hint": scatter_hint,
            "monotonic": monotonic_x,
        },
    }
    validation_reasons = input_validation_reasons(rows, first_row_is_data, profile)
    profile["valid"] = not validation_reasons
    profile["validation_reasons"] = validation_reasons
    return profile


def _is_number(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


def input_validation_reasons(
    rows: list[list[str]],
    first_row_is_data: bool | None = None,
    profile: dict[str, Any] | None = None,
) -> list[str]:
    """Mirror the build contract's non-mutating input validity checks."""

    if not rows:
        return ["No data rows found."]
    expected_columns = len(rows[0])
    for row_index, row in enumerate(rows, start=1):
        if len(row) != expected_columns:
            return [
                f"Inconsistent column count at row {row_index}: expected "
                f"{expected_columns}, found {len(row)}."
            ]

    headers, body = split_header(rows, first_row_is_data)
    header_present = has_header_row(rows, first_row_is_data)
    reasons: list[str] = []
    if not body:
        reasons.append(
            "Table must contain at least one data row; no observations were found."
        )
        return reasons
    if header_present:
        for column_index, header in enumerate(headers, start=1):
            if header == "":
                reasons.append(
                    f"Detected an empty column header at column {column_index}."
                )

    for column_index in range(expected_columns):
        values = [row[column_index] for row in body]
        if values and all(value == "" for value in values):
            header = headers[column_index] if column_index < len(headers) else ""
            label = repr(header) if header else str(column_index + 1)
            reasons.append(f"Column {label} contains no data values.")

    first_data_row = 2 if header_present else 1
    for body_index, row in enumerate(body):
        row_number = first_data_row + body_index
        for column_index, value in enumerate(row):
            if value == "":
                continue
            try:
                numeric_value = float(value)
            except ValueError:
                reasons.append(
                    f"Non-numeric value at row {row_number}, column {column_index + 1}: "
                    f"{value!r}."
                )
                continue
            if not math.isfinite(numeric_value):
                reasons.append(
                    f"Non-finite numeric value at row {row_number}, column "
                    f"{column_index + 1}: {value!r}."
                )

    if profile and profile.get("has_x_column"):
        for body_index, row in enumerate(body):
            if row[0] == "" and any(value != "" for value in row[1:]):
                row_number = first_data_row + body_index
                reasons.append(
                    f"XY observations have a missing X value at row {row_number}."
                )
    return reasons


def load_index(skill_root: Path) -> dict[str, Any]:
    path = skill_root / "assets/templates/template_index.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_curated(skill_root: Path) -> dict[str, Any]:
    path = skill_root / "assets/templates/curated_templates.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _entry_by_alias(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["alias"]: entry for entry in index.get("templates", [])}


def normalized_table_type(value: str | None) -> str | None:
    if not value:
        return None
    return {"xy": "XY", "oneway": "Column", "column": "Column", "twoway": "TwoWay"}.get(
        value.lower(), value
    )


def _entry_capacity(entry: dict[str, Any], limits: dict[str, Any] | None = None) -> int:
    entry_limits: dict[str, Any] = {}
    if limits:
        alias_limits = limits.get(str(entry.get("alias") or ""))
        if isinstance(alias_limits, dict):
            entry_limits = alias_limits
        elif "max_y_series" in limits:
            entry_limits = limits
    value = entry_limits.get("max_y_series") or entry.get("y_column_count") or 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def compatibility_result(
    entry: dict[str, Any],
    profile: dict[str, Any],
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return hard template/data compatibility using the build contract."""

    reasons = list(profile.get("validation_reasons") or [])
    if profile.get("valid") is False and not reasons:
        reasons.append("The supplied data did not pass input validation.")

    if entry.get("kind") != "xml":
        reasons.append("Template is a binary or zip bundle and cannot auto-patch data.")
    if not entry.get("table_count") or not entry.get("y_column_count"):
        reasons.append("Template has no patchable data table/Y columns.")

    entry_table = normalized_table_type(entry.get("table_type"))
    profile_table = normalized_table_type(profile.get("table_type"))
    if not entry_table:
        reasons.append("Template table type is unknown.")
    elif not profile_table:
        reasons.append("Data table type is unknown.")
    elif entry_table != profile_table:
        reasons.append(
            f"table type mismatch (data={profile_table}, template={entry_table})."
        )

    entry_has_x = entry.get("has_x_column")
    profile_has_x = profile.get("has_x_column")
    if entry_has_x is None:
        reasons.append("Template X column expectation is unknown.")
    elif profile_has_x is None:
        reasons.append("Data X column expectation is unknown.")
    elif bool(entry_has_x) != bool(profile_has_x):
        reasons.append(
            "X column expectation mismatches data "
            f"(data={bool(profile_has_x)}, template={bool(entry_has_x)})."
        )

    column_count = int(profile.get("column_count") or 0)
    requested_y = column_count - (1 if profile_has_x else 0)
    if requested_y <= 0:
        reasons.append("The supplied data contains no Y series/groups.")
    capacity = _entry_capacity(entry, limits)
    if capacity <= 0:
        reasons.append("Template has no declared Y series/group capacity.")
    elif requested_y > capacity:
        reasons.append(
            f"Template {entry.get('alias')!r} supports at most {capacity} Y "
            f"series/groups, but the data contains {requested_y}."
        )
    return {"compatible": not reasons, "reasons": reasons}


def template_search_text(entry: dict[str, Any]) -> str:
    return " ".join(
        str(entry.get(k) or "")
        for k in ("name", "alias", "source_name", "source_alias", "first_title")
    ).lower()


def semantic_terms(value: str) -> set[str]:
    text = normalize_hint_text(value)
    for source, target in {
        "水彩": " watercolor ",
        "小提琴": " violin ",
        "雨云": " raincloud ",
        "多组": " multiple ",
        "多因子": " grouped ",
        "热图": " heatmap ",
        "热力图": " heatmap ",
        "相关性": " correlation ",
    }.items():
        text = text.replace(source, target)
    return {
        x
        for x in re.findall(r"[a-z0-9]+", text)
        if len(x) >= 3
        and not x.isdecimal()
        and x not in {"plot", "graph", "template", "with", "the", "converted"}
    }


def matches_query(entry, query):
    identity = resolve_identity([entry], query)
    if identity["kind"] != "none":
        return bool(identity["candidates"])
    if requested_template_number(query) is not None:
        return False
    query = query.strip().casefold()
    text = template_search_text(entry)
    if not query:
        return True
    if query in text:
        return True
    terms = semantic_terms(query)
    return bool(terms) and terms.issubset(semantic_terms(text))


def score_template(
    entry: dict[str, Any],
    profile: dict[str, Any],
    hint_text: str,
    whitelist: set[str],
    curated_bonus: int = 0,
    curated_types: list[str] | None = None,
    default_bonus: int = 0,
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    if entry.get("kind") != "xml":
        return -1000, ["binary or zip bundle; cannot auto-patch data"]
    if not entry.get("table_count") or not entry.get("y_column_count"):
        return -1000, ["template has no patchable data table/Y columns"]
    if entry.get("collection") == "verified":
        score += 20
        reasons.append("render-verified managed template")
    if entry.get("native_titles"):
        score += 45
        reasons.append("native labels and adapted user style")
    if entry.get("validation") == "ok":
        score += 20
        reasons.append("validated in Prism")
    if entry["alias"] in whitelist:
        score += 40
        reasons.append("automation whitelist")

    entry_table = normalized_table_type(entry.get("table_type"))
    if entry_table and profile["table_type"] and entry_table == profile["table_type"]:
        score += 35
        reasons.append(f"table type matches ({entry_table})")
    elif entry_table and profile["table_type"] and entry_table != profile["table_type"]:
        score -= 40
        reasons.append(
            f"table type mismatch (data={profile['table_type']}, template={entry_table})"
        )

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

    if curated_bonus:
        score += curated_bonus
        labels = ", ".join(curated_types or [])
        reasons.append(f"curated hint match ({labels}) +{curated_bonus}")
    if default_bonus:
        score += default_bonus
        reasons.append(f"safe default +{default_bonus}")

    haystack = template_search_text(entry)
    overlap = semantic_terms(hint_text) & semantic_terms(haystack)
    if overlap:
        score += len(overlap) * 100
        reasons.append("name/style match: " + ", ".join(sorted(overlap)))
    exact_fields = [
        str(entry.get(k) or "").lower() for k in ("alias", "source_name", "name")
    ]
    if hint_text.strip() and hint_text.lower() in exact_fields:
        score += 2000
        reasons.append("explicit template identity")

    hint = normalize_hint_text(hint_text)
    if entry.get("alias") == "volcano-plot" and not any(
        phrase in hint
        for phrase in ("volcano", "differential expression", "log fold", "fold change")
    ):
        score -= 50
        reasons.append("volcano template requires volcano/differential-expression hint")

    return score, reasons


def curated_bonuses(
    hint_text: str,
    curated: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, tuple[int, list[str]]]:
    raw_hint = hint_text.lower()
    normalized_hint = normalize_hint_text(hint_text)
    bonuses: dict[str, tuple[int, list[str]]] = {}
    candidates: list[tuple[dict[str, Any], list[str], list[str]]] = []
    for figure in curated.get("figure_types", []):
        keywords = figure.get("keywords", [])
        raw_matches = [keyword for keyword in keywords if keyword.lower() in raw_hint]
        normalized_matches = [
            keyword for keyword in keywords if keyword.lower() in normalized_hint
        ]
        if normalized_table_type(figure.get("table_type")) != profile["table_type"]:
            continue
        if bool(figure.get("has_x_column")) != profile["has_x_column"]:
            continue
        candidates.append((figure, raw_matches, normalized_matches))

    prefer_raw = any(raw_matches for _, raw_matches, _ in candidates)
    for figure, raw_matches, normalized_matches in candidates:
        matched_keywords = raw_matches if prefer_raw else normalized_matches
        if not matched_keywords:
            continue
        specificity = max(len(keyword) for keyword in matched_keywords)
        bonus = 75 + len(matched_keywords) * 10 + specificity * 8
        aliases = [figure["template"], *figure.get("compatible_templates", [])]
        for alias in aliases:
            old_bonus, old_types = bonuses.get(alias, (0, []))
            bonuses[alias] = (max(old_bonus, bonus), [*old_types, figure["id"]])
    return bonuses


def curated_hint_matches(
    hint_text: str, curated: dict[str, Any]
) -> list[dict[str, Any]]:
    raw_hint = hint_text.lower()
    normalized_hint = normalize_hint_text(hint_text)
    matches: list[dict[str, Any]] = []
    for figure in curated.get("figure_types", []):
        if any(
            keyword.lower() in raw_hint or keyword.lower() in normalized_hint
            for keyword in figure.get("keywords", [])
        ):
            matches.append(figure)
    return matches


def conservative_default_alias(profile: dict[str, Any]) -> str:
    return "watercolor-xy-lines" if profile["has_x_column"] else "watercolor-points"


def unsupported_automation_match(
    hint_text: str, curated: dict[str, Any]
) -> dict[str, Any] | None:
    raw_hint = hint_text.lower()
    if raw_hint in curated.get("whitelist_automation", []):
        return None
    if any(w in raw_hint for w in ("bar", "柱状", "柱形")) and (
        re.search(r"(?<![a-z])(?:sd|ci)(?![a-z])|standard deviation|stdev", raw_hint)
        or "标准差" in raw_hint
        or "置信区间" in raw_hint
    ):
        return {
            "id": "unsupported_uncertainty",
            "reason": "The managed bar template plots SEM, not the requested SD/CI uncertainty. Choose a matching native template.",
        }
    normalized_hint = normalize_hint_text(hint_text)
    hits: list[tuple[int, int, dict[str, Any]]] = []
    for item in curated.get("unsupported_automation", []):
        if item.get("id") == "xy_scatter" and any(
            safe_phrase in raw_hint
            for safe_phrase in ("列散点图", "column scatter", "dot plot", "水彩散点")
        ):
            if not any(
                w in raw_hint
                for w in ("correlation", "相关性", "相关散点", "xy scatter")
            ):
                continue
        keywords = item.get("keywords", [])
        raw_matches = [keyword for keyword in keywords if keyword.lower() in raw_hint]
        normalized_matches = [
            keyword for keyword in keywords if keyword.lower() in normalized_hint
        ]
        matched_keywords = raw_matches or normalized_matches
        if matched_keywords:
            hits.append(
                (
                    max(len(keyword) for keyword in matched_keywords),
                    len(matched_keywords),
                    item,
                )
            )
    if not hits:
        return None
    hits.sort(key=lambda item: (-item[0], -item[1], item[2].get("id", "")))
    # Release only figure types with a real managed replacement. Significance,
    # survival and unconfigured analyses remain blocked regardless of wording.
    resolved = {
        "box_whisker": "watercolor_box",
        "before_after": "paired",
        "spaghetti": "paired",
        "scatter_bars": "bars_sem",
    }
    blocked_id = hits[0][2].get("id")
    if blocked_id in resolved and not any(
        h[2].get("id") == "box_whisker_significance" for h in hits
    ):
        approved = next(
            (
                f
                for f in curated.get("figure_types", [])
                if f["id"] == resolved[blocked_id]
            ),
            None,
        )
        if approved and any(
            k.lower() in raw_hint or k.lower() in normalized_hint
            for k in approved["keywords"]
        ):
            return None
    return hits[0][2]


def _identity_candidate_summary(
    entry: dict[str, Any], whitelist: set[str]
) -> dict[str, Any]:
    return {
        "alias": entry.get("alias"),
        "name": entry.get("name"),
        "source_name": entry.get("source_name") or entry.get("name"),
        "collection": entry.get("collection"),
        "relative_path": entry.get("relative_path"),
        "table_type": entry.get("table_type"),
        "has_x_column": entry.get("has_x_column"),
        "y_column_count": entry.get("y_column_count"),
        "automation_status": (
            "safe_automation" if entry.get("alias") in whitelist else "reference_only"
        ),
    }


def _blocked_response(
    profile: dict[str, Any],
    hint_text: str,
    automation_only: bool,
    blocked_type: str,
    reason: str,
    identity: dict[str, Any] | None = None,
    whitelist: set[str] | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "data_profile": profile,
        "hint": hint_text,
        "automation_only": automation_only,
        "matches": [],
        "recommended": None,
        "blocked_figure_type": blocked_type,
        "blocked_reason": reason,
    }
    if identity and identity.get("kind") != "none":
        allowed = whitelist or set()
        response.update(
            {
                "requested_identity": identity.get("requested"),
                "identity_kind": identity.get("kind"),
                "identity_candidates": [
                    _identity_candidate_summary(entry, allowed)
                    for entry in identity.get("candidates", [])
                ],
            }
        )
    return response


def _compatibility_block_type(profile: dict[str, Any], reasons: list[str]) -> str:
    if profile.get("valid") is False:
        return "invalid_data"
    if any("supports at most" in reason for reason in reasons):
        return "template_capacity"
    if any(
        "table type mismatch" in reason.lower() or "X column expectation" in reason
        for reason in reasons
    ):
        return "data_shape_mismatch"
    return "template_incompatible"


def match_templates(
    skill_root: Path,
    data_path: Path,
    hint: str = "",
    limit: int = 5,
    automation_only: bool = True,
    first_row_is_data: bool | None = None,
) -> dict[str, Any]:
    rows = read_table(data_path)
    hint_text = hint.strip()
    profile = infer_data_profile(rows, hint_text, first_row_is_data)
    index = load_index(skill_root)
    curated = load_curated(skill_root)
    whitelist = set(curated.get("whitelist_automation", []))
    limits = curated.get("automation_limits", {})
    entries = index.get("templates", [])
    identity = resolve_identity(entries, hint_text)

    if identity["kind"] == "ambiguous":
        aliases = ", ".join(
            str(entry.get("alias") or "") for entry in identity["candidates"]
        )
        return _blocked_response(
            profile,
            hint_text,
            automation_only,
            "ambiguous_template_identity",
            f"Template identity {hint_text!r} matches multiple catalog entries: {aliases}.",
            identity,
            whitelist,
        )

    if identity["kind"] == "number" and not identity["candidates"]:
        return _blocked_response(
            profile,
            hint_text,
            automation_only,
            "unknown_template_identity",
            f"No catalog entry has original template number {hint_text!r}.",
            identity,
            whitelist,
        )

    selected = (
        identity["candidates"][0]
        if identity["kind"] in {"exact", "number"} and identity["candidates"]
        else None
    )
    if selected and automation_only and selected.get("alias") not in whitelist:
        return _blocked_response(
            profile,
            hint_text,
            automation_only,
            "reference_only_template",
            (
                f"Template {selected.get('alias')!r} is available as a reference design "
                "but is not approved for automated data replacement."
            ),
            identity,
            whitelist,
        )

    if selected:
        selected_compatibility = compatibility_result(selected, profile, limits)
        if not selected_compatibility["compatible"]:
            reasons = selected_compatibility["reasons"]
            return _blocked_response(
                profile,
                hint_text,
                automation_only,
                _compatibility_block_type(profile, reasons),
                " ".join(reasons),
                identity,
                whitelist,
            )
        bonuses = {selected["alias"]: (2000, ["explicit_template"])}
    else:
        bonuses = curated_bonuses(hint_text, curated, profile)

    if profile.get("valid") is False:
        return _blocked_response(
            profile,
            hint_text,
            automation_only,
            "invalid_data",
            " ".join(profile.get("validation_reasons") or ["Invalid input data."]),
        )
    default_alias = conservative_default_alias(profile)

    blocked = (
        unsupported_automation_match(hint_text, curated) if automation_only else None
    )
    if blocked is not None:
        return _blocked_response(
            profile,
            hint_text,
            automation_only,
            blocked.get("id"),
            blocked.get("reason"),
        )

    matched_figures = curated_hint_matches(hint_text, curated)
    if automation_only and matched_figures and not bonuses:
        expected = sorted(
            {
                f"{normalized_table_type(item.get('table_type'))}"
                + (" with X" if item.get("has_x_column") else " without X")
                for item in matched_figures
            }
        )
        return _blocked_response(
            profile,
            hint_text,
            automation_only,
            "data_shape_mismatch",
            (
                f"The figure hint matches a curated type that expects {', '.join(expected)}, "
                f"but the supplied data was classified as {profile['table_type']}."
            ),
        )

    target_aliases = set(bonuses) if bonuses else {default_alias}
    indexed_by_alias = _entry_by_alias(index)
    target_results = [
        compatibility_result(indexed_by_alias[alias], profile, limits)
        for alias in target_aliases
        if alias in indexed_by_alias
    ]
    if automation_only and (
        not target_results or not any(item["compatible"] for item in target_results)
    ):
        target_reasons = list(
            dict.fromkeys(
                reason for item in target_results for reason in item.get("reasons", [])
            )
        )
        if not target_reasons:
            target_reasons = ["The requested template is unavailable in the catalog."]
        return _blocked_response(
            profile,
            hint_text,
            automation_only,
            _compatibility_block_type(profile, target_reasons),
            " ".join(target_reasons),
            identity if selected else None,
            whitelist,
        )

    candidates = index.get("templates", [])
    if automation_only:
        candidates = [entry for entry in candidates if entry.get("alias") in whitelist]

    scored: list[tuple[int, dict[str, Any], list[str]]] = []
    for entry in candidates:
        if not compatibility_result(entry, profile, limits)["compatible"]:
            continue
        bonus, figure_types = bonuses.get(entry["alias"], (0, []))
        default_bonus = (
            70
            if automation_only and not bonuses and entry["alias"] == default_alias
            else 0
        )
        score, reasons = score_template(
            entry,
            profile,
            hint_text,
            whitelist,
            curated_bonus=bonus,
            curated_types=figure_types,
            default_bonus=default_bonus,
        )
        if score < 0:
            continue
        if bonuses and entry["alias"] not in bonuses:
            score -= 35
            reasons.append("not the curated target for this hint")
        scored.append((score, entry, reasons))
    scored.sort(key=lambda item: (-item[0], item[1]["alias"]))

    results: list[dict[str, Any]] = []
    for score, entry, reasons in scored:
        results.append(
            {
                "alias": entry["alias"],
                "name": entry.get("name"),
                "source_name": entry.get("source_name") or entry.get("name"),
                "automation_status": (
                    "safe_automation"
                    if entry["alias"] in whitelist
                    else "experimental_preview"
                ),
                "score": score,
                "source": "ranked",
                "figure_types": bonuses.get(entry["alias"], (0, []))[1],
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
        "ambiguous": not bool(selected)
        and not bool(bonuses)
        and not any(
            "name/style match" in r or "explicit template identity" in r
            for r in (results[0]["reasons"] if results else [])
        ),
        "needs_hint": not bool(hint_text),
        "selection_basis": (
            f"{identity['kind']}_identity"
            if selected
            else (
                "curated_hint"
                if bonuses
                else "catalog_search" if not automation_only else "safe_default"
            )
        ),
    }
