"""EEBUS JSON encoding.

SHIP and SPINE do not use plain JSON objects. Every object is written as an
array of single-key objects, while real arrays stay arrays:

    {"a": 1, "b": {"c": 2}}   ->   [{"a": 1}, {"b": [{"c": 2}]}]
    {"list": [{"x": 1}]}      ->   [{"list": [[{"x": 1}]]}]

The top level message has one key and is written without the outer array:
``{"connectionHello": [{"phase": "ready"}]}``.

This module converts structurally (not by string replacement as ship-go does),
so string values containing brackets are safe.
"""

from __future__ import annotations

import json
from typing import Any


def _encode(value: Any) -> Any:
    if isinstance(value, dict):
        return [{key: _encode(item)} for key, item in value.items()]
    if isinstance(value, list):
        return [_encode(item) for item in value]
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        if not value:
            # "[]" is how an empty object is sent (e.g. accessMethodsRequest).
            return {}
        if all(isinstance(item, dict) for item in value):
            merged: dict[str, Any] = {}
            for item in value:
                for key, inner in item.items():
                    merged[key] = _decode(inner)
            return merged
        return [_decode(item) for item in value]
    return value


def encode(message: dict[str, Any]) -> str:
    """Plain dict -> EEBUS JSON text (top level object kept as object)."""
    if not isinstance(message, dict):
        raise TypeError("top level EEBUS message must be a dict")
    return json.dumps({key: _encode(value) for key, value in message.items()},
                      separators=(",", ":"), ensure_ascii=False)


def decode(data: bytes | str) -> dict[str, Any]:
    """EEBUS JSON text -> plain dict."""
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    data = data.strip("\x00").strip()
    parsed = json.loads(data)
    if isinstance(parsed, list):
        # Some implementations keep the outer array.
        parsed = _decode(parsed)
    elif isinstance(parsed, dict):
        parsed = _decode(parsed)
    else:
        raise ValueError("EEBUS message is not an object")
    if not isinstance(parsed, dict):
        raise ValueError("EEBUS message is not an object")
    return parsed
