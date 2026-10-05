#!/usr/bin/env python3
"""Resolve explicit Prism template identities without fuzzy numeric fallbacks."""

from __future__ import annotations

import re
from typing import Any, Iterable

_NUMBER_QUERIES = (
    re.compile(r"^\s*0*(\d+)\s*$", re.IGNORECASE),
    re.compile(
        r"^\s*template(?:[\s_-]*(?:number|no\.?))?\s*[:：#-]?\s*0*(\d+)\s*$",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*(?:模板|模版)(?:\s*(?:编号|号码|号))?\s*[:：#-]?\s*0*(\d+)\s*$"),
    re.compile(r"^\s*第?\s*0*(\d+)\s*号?\s*(?:模板|模版)\s*$"),
)
_ORIGINAL_NUMBER = re.compile(r"^\s*0*(\d+)\s*[.。]")


def requested_template_number(query: Any) -> str | None:
    """Return a normalized number only for a complete template-number query."""

    if not isinstance(query, (str, int)) or isinstance(query, bool):
        return None
    text = str(query)
    for pattern in _NUMBER_QUERIES:
        match = pattern.fullmatch(text)
        if match:
            return str(int(match.group(1)))
    return None


def _original_template_number(entry: dict[str, Any]) -> str | None:
    """Read a human source-number prefix, never a transport filename component."""

    for field in ("source_name", "name"):
        value = str(entry.get(field) or "")
        match = _ORIGINAL_NUMBER.match(value)
        if match:
            return str(int(match.group(1)))
    return None


def _unique(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[int] = set()
    for entry in entries:
        marker = id(entry)
        if marker not in seen:
            seen.add(marker)
            result.append(entry)
    return result


def _numbered_family(
    catalog: list[dict[str, Any]], number: str
) -> list[dict[str, Any]]:
    direct = [entry for entry in catalog if _original_template_number(entry) == number]
    if not direct:
        return []
    lineage = {
        str(entry.get(field) or "").strip().casefold()
        for entry in direct
        for field in ("alias", "source_alias")
        if entry.get(field)
    }
    included = {id(entry) for entry in direct}
    changed = True
    while changed:
        changed = False
        for entry in catalog:
            alias = str(entry.get("alias") or "").strip().casefold()
            source_alias = str(entry.get("source_alias") or "").strip().casefold()
            if id(entry) not in included and (
                alias in lineage or source_alias in lineage
            ):
                included.add(id(entry))
                if alias:
                    lineage.add(alias)
                if source_alias:
                    lineage.add(source_alias)
                changed = True
    return [entry for entry in catalog if id(entry) in included]


def resolve_identity(entries: Iterable[dict[str, Any]], query: Any) -> dict[str, Any]:
    """Resolve an alias, human name, or original numeric template identifier.

    Alias identity has priority over human-facing names. Numeric identity is
    accepted only when the entire query is a bare number or an explicit
    template-number phrase. Multiple exact candidates are returned as an
    ambiguity instead of selecting one by catalog order.
    """

    catalog = list(entries)
    requested = query
    text = str(query).strip() if query is not None else ""
    folded = text.casefold()
    if not folded:
        return {"kind": "none", "candidates": [], "requested": requested}

    aliases = _unique(
        entry
        for entry in catalog
        if str(entry.get("alias") or "").strip().casefold() == folded
    )
    if aliases:
        return {
            "kind": "exact" if len(aliases) == 1 else "ambiguous",
            "candidates": aliases,
            "requested": requested,
        }

    human_names = _unique(
        entry
        for entry in catalog
        if any(
            str(entry.get(field) or "").strip().casefold() == folded
            for field in ("name", "source_name")
        )
        or (isinstance(entry.get("aliases"), list) and any(
            isinstance(alias, str) and alias.strip().casefold() == folded
            for alias in entry["aliases"]
        ))
    )
    if human_names:
        return {
            "kind": "exact" if len(human_names) == 1 else "ambiguous",
            "candidates": human_names,
            "requested": requested,
        }

    number = requested_template_number(query)
    if number is None:
        return {"kind": "none", "candidates": [], "requested": requested}
    numbered = _numbered_family(catalog, number)
    if not numbered:
        return {"kind": "number", "candidates": [], "requested": requested}
    return {
        "kind": "number" if len(numbered) == 1 else "ambiguous",
        "candidates": numbered,
        "requested": requested,
    }
