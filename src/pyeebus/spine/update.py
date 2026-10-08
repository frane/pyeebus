"""SPINE data updates with filters (SPINE 5.3.4), ported from spine-go's model/update.go.

A function's data is a dict such as ``{"measurementData": [...]}``. Full
replies/notifies replace the data. Partial notifies and deletes merge list
items by their key fields.
"""

from __future__ import annotations

import copy
from typing import Any

from . import schema
from .model import as_list


def filter_parts(filters: list[dict[str, Any]] | None) -> tuple[dict | None, dict | None]:
    """Split a cmd's filter list into (partial filter, delete filter)."""
    partial = delete = None
    for f in as_list(filters):
        control = f.get("cmdControl") or {}
        if "partial" in control:
            partial = f
        if "delete" in control:
            delete = f
    return partial, delete


def _selector(function: str, filt: dict[str, Any] | None) -> dict[str, Any] | None:
    if not filt:
        return None
    name = schema.SELECTORS.get(function)
    value = filt.get(name) if name else None
    return value if isinstance(value, dict) and value else None


def _elements(function: str, filt: dict[str, Any] | None) -> dict[str, Any] | None:
    if not filt:
        return None
    name = schema.ELEMENTS.get(function)
    value = filt.get(name) if name else None
    return value if isinstance(value, dict) else None


def _selector_match(item: dict[str, Any], selector: dict[str, Any]) -> bool:
    return all(item.get(k) == v for k, v in selector.items())


def _key(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    return repr([item.get(k) for k in keys])


def _has_identifiers(item: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return all(item.get(k) is not None for k in keys)


def _has_value(value: Any) -> bool:
    return value is not None and value != [] and value != {} and value != ""


def _key_only(item: dict[str, Any], keys: tuple[str, ...], primary: tuple[str, ...]) -> bool:
    if len(primary) > 1 or (primary and len(keys) > 1):
        id_fields = primary
    elif len(keys) == 1:
        id_fields = keys
    else:
        return False
    present = [k for k, v in item.items() if _has_value(v)]
    return bool(present) and all(k in id_fields for k in present)


def _merge(existing: list[dict], new: list[dict], keys: tuple[str, ...]) -> list[dict]:
    by_key = {_key(item, keys): item for item in new}
    seen = set()
    result = []
    for old in existing:
        k = _key(old, keys)
        seen.add(k)
        if k in by_key:
            merged = {**old, **{f: v for f, v in by_key[k].items() if v is not None}}
            result.append(merged)
        else:
            result.append(old)
    result.extend(item for item in new if _key(item, keys) not in seen)
    return result


def _sort(items: list[dict], keys: tuple[str, ...]) -> list[dict]:
    def sort_key(item: dict) -> tuple:
        return tuple((0, v) if isinstance(v, int) else (1, 0) for v in (item.get(k) for k in keys))
    try:
        return sorted(items, key=sort_key)
    except TypeError:
        return items


def update_data(function: str, existing: dict[str, Any] | None, new: dict[str, Any] | None,
                filters: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Return the function data after applying ``new`` with the cmd's filters."""
    partial, delete = filter_parts(filters)
    if partial is None and delete is None:
        return copy.deepcopy(new) if new is not None else None
    spec = schema.LIST_FUNCTIONS.get(function)
    if spec is None:
        # not a list: a partial update just overwrites the given fields
        return {**(existing or {}), **(new or {})}
    field, keys, primary = spec
    items = [dict(i) for i in as_list((existing or {}).get(field))]
    new_items = [dict(i) for i in as_list((new or {}).get(field))]

    # 1. delete filter
    if delete is not None:
        selector, elements = _selector(function, delete), _elements(function, delete)
        if selector is not None or elements is not None:
            kept = []
            for item in items:
                if selector is not None and not _selector_match(item, selector):
                    kept.append(item)
                    continue
                if elements is not None:
                    for name in elements:
                        item.pop(name, None)
                    kept.append(item)
                # selector only: drop the whole item
            items = kept

    # 2. partial filter with selector: copy into the first matching item
    if partial is not None:
        selector = _selector(function, partial)
        if selector is not None and new_items:
            for item in items:
                if _selector_match(item, selector):
                    item.update({k: v for k, v in new_items[0].items() if v is not None})
                    break
            return {field: items}

    # 3. ignore entries that only carry their identifiers
    new_items = [i for i in new_items if not _key_only(i, keys, primary)]
    if not new_items:
        return {field: items}

    # 4. no complete identifiers: apply to all items
    if keys and not _has_identifiers(new_items[0], keys):
        for item in items:
            item.update({k: v for k, v in new_items[0].items() if v is not None})
        return {field: items}

    # 5./6. merge by key and sort
    return {field: _sort(_merge(items, new_items, keys), keys)}
