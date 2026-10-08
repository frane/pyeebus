"""SHIP message types and constants (SHIP 1.0.1)."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any

from . import eebus_json

PROTOCOL_ID = "ee1.0"
SUBPROTOCOL = "ship"  # SHIP 10.2
WEBSOCKET_PATH = "/ship/"
SERVICE_TYPE = "_ship._tcp.local."
DEFAULT_PORT = 4712

# Timers (seconds)
CMI_TIMEOUT = 10.0  # SHIP 4.2
HELLO_INIT_TIMEOUT = 60.0  # SHIP 13.4.4.1.3 T_hello_init
HELLO_INC_TIMEOUT = HELLO_INIT_TIMEOUT
HELLO_PROLONGATIONS_ALWAYS_ACCEPTED = 2
PROTOCOL_TIMEOUT = 10.0
PIN_TIMEOUT = 10.0
ACCESS_METHODS_TIMEOUT = 60.0  # SHIP 13.4.6.2.1
PING_INTERVAL = 50.0  # SHIP 4.2

INIT_MESSAGE = b"\x00\x00"

CLOSE_REJECTED = 4452  # "Node rejected by application": remote has not paired us


class MsgType(IntEnum):
    INIT = 0
    CONTROL = 1
    DATA = 2
    END = 3


class Role(StrEnum):
    CLIENT = "client"
    SERVER = "server"


class State(StrEnum):
    """Coarse connection state reported to callers."""

    CMI = "cmi"
    HELLO = "hello"
    HELLO_WAITING_FOR_TRUST = "hello_waiting_for_trust"  # remote has not trusted us yet
    PROTOCOL = "protocol"
    PIN = "pin"
    ACCESS_METHODS = "access_methods"
    COMPLETE = "complete"
    CLOSED = "closed"
    ERROR = "error"


class ShipError(Exception):
    """Handshake or protocol failure."""


class RemoteAbortError(ShipError):
    """The remote node aborted the hello phase or rejected us (it does not trust us)."""


def control(message: dict[str, Any]) -> bytes:
    return bytes([MsgType.CONTROL]) + eebus_json.encode(message).encode()


def end(message: dict[str, Any]) -> bytes:
    return bytes([MsgType.END]) + eebus_json.encode(message).encode()


def data(payload: dict[str, Any]) -> bytes:
    """SHIP data message carrying a SPINE payload.

    Matches ship-go byte for byte: the payload's top level object is written as
    an object (``"payload":{"datagram":[...]}``), not as an array.
    """
    header = eebus_json.encode({"header": {"protocolId": PROTOCOL_ID}})[1:-1]
    body = eebus_json.encode(payload)
    text = '{"data":[' + "{" + header + "}," + '{"payload":' + body + "}]}"
    return bytes([MsgType.DATA]) + text.encode()


def parse(message: bytes) -> tuple[int, dict[str, Any] | None]:
    """Split a websocket message into SHIP type byte and decoded JSON (if any)."""
    if not message:
        raise ShipError("empty SHIP message")
    kind = message[0]
    body = message[1:]
    if kind == MsgType.INIT:
        return kind, None
    return kind, eebus_json.decode(body)


def hello(phase: str, waiting_ms: int | None = None, prolongation: bool = False) -> bytes:
    body: dict[str, Any] = {"phase": phase}
    if waiting_ms:
        body["waiting"] = int(waiting_ms)
    if prolongation:
        body["prolongationRequest"] = True
    return control({"connectionHello": body})


def protocol_handshake(handshake_type: str) -> bytes:
    return control({
        "messageProtocolHandshake": {
            "handshakeType": handshake_type,
            "version": {"major": 1, "minor": 0},
            "formats": {"format": ["JSON-UTF8"]},
        }
    })


def protocol_handshake_error(code: int) -> bytes:
    return control({"messageProtocolHandshakeError": {"error": code}})


def pin_state_none() -> bytes:
    return control({"connectionPinState": {"pinState": "none"}})


def access_methods_request() -> bytes:
    return control({"accessMethodsRequest": {}})


def access_methods(ship_id: str) -> bytes:
    return control({"accessMethods": {"id": ship_id}})


def connection_close(phase: str, reason: str | None = None, max_time_ms: int | None = None) -> bytes:
    body: dict[str, Any] = {"phase": phase}
    if max_time_ms is not None:
        body["maxTime"] = max_time_ms
    if reason is not None:
        body["reason"] = reason
    return end({"connectionClose": body})
