"""SPINE basics: constants, addresses and value helpers.

SPINE data is kept as plain dicts with the SPINE JSON names (as decoded by
:mod:`pyeebus.ship.eebus_json`). This module only adds the bits needed to
work with them.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

SPECIFICATION_VERSION = "1.3.0"
DEFAULT_MAX_RESPONSE_DELAY = 10.0  # seconds


class CmdClassifier:
    READ = "read"
    REPLY = "reply"
    NOTIFY = "notify"
    WRITE = "write"
    CALL = "call"
    RESULT = "result"


class Role:
    CLIENT = "client"
    SERVER = "server"
    SPECIAL = "special"


class ErrorNumber:
    NO_ERROR = 0
    GENERAL_ERROR = 1
    TIMEOUT = 2
    OVERLOAD = 3
    DESTINATION_UNKNOWN = 4
    DESTINATION_UNREACHABLE = 5
    COMMAND_NOT_SUPPORTED = 6
    COMMAND_REJECTED = 7
    RESTRICTED_FUNCTION_EXCHANGE_COMBINATION_NOT_SUPPORTED = 8
    BINDING_IS_NECESSARY_FOR_THIS_COMMAND = 9


class SpineError(Exception):
    """A SPINE result error (``resultData`` with an error number)."""

    def __init__(self, number: int, description: str | None = None) -> None:
        super().__init__(f"SPINE error {number}" + (f": {description}" if description else ""))
        self.number = number
        self.description = description


# --- addresses ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Address:
    """A SPINE feature (or entity, with ``feature=None``) address."""

    device: str | None
    entity: tuple[int, ...]
    feature: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Address:
        data = data or {}
        entity = data.get("entity")
        return cls(data.get("device"), tuple(entity) if isinstance(entity, list) else (),
                   data.get("feature"))

    def to_dict(self, with_device: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if with_device and self.device is not None:
            out["device"] = self.device
        out["entity"] = list(self.entity)
        if self.feature is not None:
            out["feature"] = self.feature
        return out

    @property
    def entity_address(self) -> Address:
        return Address(self.device, self.entity)

    def with_device(self, device: str | None) -> Address:
        return Address(device, self.entity, self.feature)

    def __str__(self) -> str:
        entity = ".".join(str(e) for e in self.entity)
        feature = "" if self.feature is None else f":{self.feature}"
        return f"{self.device or '?'}:[{entity}]{feature}"


def device_address(brand: str, model: str, serial: str = "", vendor: str | None = None) -> str:
    """SPINE device address (SPINE 7.1.1.2), like eebus-go: ``d:_n:<vendor>_<model>-<serial>``."""
    vendor = vendor or brand
    vendor_type = "i" if vendor.isdigit() else "n"
    return f"d:_{vendor_type}:{vendor}_{model}" + (f"-{serial}" if serial else "")


# --- values -----------------------------------------------------------------------


def as_list(value: Any) -> list[Any]:
    """SPINE lists may arrive as ``{}`` (EEBUS JSON encodes an empty list as ``[]``,
    which decodes to an empty object) or be absent."""
    if value is None or value == {}:
        return []
    if isinstance(value, list):
        return value
    return [value]


def scaled_value(number: dict[str, Any] | None) -> float | None:
    """Value of a ScaledNumberType ``{"number": n, "scale": s}``."""
    if not number or number.get("number") is None:
        return None
    return number["number"] * 10 ** number.get("scale", 0)


def scaled_number(value: float) -> dict[str, int]:
    """ScaledNumberType for a value (max. 4 decimals, as in spine-go)."""
    text = repr(float(value))
    decimals = 0
    if "e" not in text and "." in text:
        decimals = min(len(text.split(".")[1].rstrip("0")), 4)
    number = math.trunc(round(value * 10**decimals, 6))
    return {"number": number, "scale": -decimals if number else 0}


_DURATION = re.compile(
    r"^(?P<sign>-)?P(?:(?P<y>\d+(?:\.\d+)?)Y)?(?:(?P<mo>\d+(?:\.\d+)?)M)?(?:(?P<w>\d+(?:\.\d+)?)W)?"
    r"(?:(?P<d>\d+(?:\.\d+)?)D)?(?:T(?:(?P<h>\d+(?:\.\d+)?)H)?(?:(?P<mi>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<s>\d+(?:\.\d+)?)S)?)?$")


def parse_duration(text: str) -> float:
    """ISO 8601 duration (``PT4S``) to seconds."""
    m = _DURATION.match(text.strip())
    if not m:
        raise ValueError(f"invalid duration {text!r}")
    parts = {k: float(v) for k, v in m.groupdict().items() if v and k != "sign"}
    seconds = (parts.get("y", 0) * 365 * 86400 + parts.get("mo", 0) * 30 * 86400
               + parts.get("w", 0) * 7 * 86400 + parts.get("d", 0) * 86400
               + parts.get("h", 0) * 3600 + parts.get("mi", 0) * 60 + parts.get("s", 0))
    return -seconds if m.group("sign") else seconds


def format_duration(seconds: float) -> str:
    """Seconds to an ISO 8601 duration without months/years (as spine-go)."""
    sign = "-" if seconds < 0 else ""
    total = abs(seconds)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    out = f"{sign}P"
    if days:
        out += f"{int(days)}D"
    time_part = ""
    if hours:
        time_part += f"{int(hours)}H"
    if minutes:
        time_part += f"{int(minutes)}M"
    if secs or not (days or hours or minutes):
        time_part += f"{secs:g}S"
    return out + ("T" + time_part if time_part else "")


def format_datetime(value: dt.datetime | None = None) -> str:
    value = (value or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    if value.microsecond >= 500_000:
        value += dt.timedelta(seconds=1)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_datetime(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text)


def matches(item: dict[str, Any], selector: dict[str, Any]) -> bool:
    """True if every field of ``selector`` equals the field in ``item``."""
    return all(item.get(k) == v for k, v in selector.items() if v is not None)


def filter_items(items: Iterable[dict[str, Any]], selector: dict[str, Any] | None = None,
                 **fields: Any) -> list[dict[str, Any]]:
    selector = {**(selector or {}), **fields}
    return [item for item in items if matches(item, selector)]
