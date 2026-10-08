"""SHIP connection: handshake and data exchange over one websocket.

Port of ship-go's ShipConnection (MIT, enbility). The event driven state machine
of ship-go is written here as a linear asyncio sequence; the message flow and
timers follow SHIP 1.0.1 and ship-go.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from . import model
from .model import MsgType, RemoteAbortError, Role, ShipError, State

_LOGGER = logging.getLogger(__name__)


class Transport(Protocol):
    """Binary websocket: one SHIP message per frame."""

    async def send(self, message: bytes) -> None: ...
    async def receive(self) -> bytes:  # raises ConnectionError when closed
        ...
    async def close(self, code: int = 4001, reason: str = "") -> None: ...


DataHandler = Callable[[dict[str, Any]], Awaitable[None] | None]
StateHandler = Callable[["ShipConnection", State, Exception | None], None]


class ShipConnection:
    """One SHIP connection to a remote node."""

    def __init__(
        self,
        transport: Transport,
        role: Role,
        local_ship_id: str,
        remote_ski: str,
        remote_ship_id: str | None = None,
        *,
        allow_waiting_for_trust: Callable[[], bool] = lambda: True,
        on_state: StateHandler | None = None,
    ) -> None:
        self.transport = transport
        self.role = role
        self.local_ship_id = local_ship_id
        self.remote_ski = remote_ski
        self.remote_ship_id = remote_ship_id
        self.state = State.CMI
        self.error: Exception | None = None
        self._on_data: DataHandler | None = None
        self._early_data: list[dict[str, Any]] = []  # received before on_data was set
        self._allow_waiting_for_trust = allow_waiting_for_trust
        self._on_state = on_state
        self._inbox: asyncio.Queue[bytes | Exception] = asyncio.Queue()
        self._reader: asyncio.Task | None = None
        self._closed = asyncio.Event()
        self._access_methods_done = asyncio.Event()

    # --- public ------------------------------------------------------------

    @property
    def is_complete(self) -> bool:
        return self.state == State.COMPLETE

    async def run(self) -> None:
        """Perform the handshake; returns once data can be exchanged.

        Raises ShipError (or RemoteAbortError) on failure. Afterwards incoming
        SPINE payloads are passed to ``on_data`` until the connection closes.
        """
        self._reader = asyncio.create_task(self._read_loop(), name=f"ship-read-{self.remote_ski[:8]}")
        try:
            await self._cmi()
            await self._hello()
            await self._protocol()
            await self._pin()
            await self._access_methods()
        except Exception as err:
            await self._fail(err)
            raise
        self._set_state(State.COMPLETE)

    async def send_spine(self, payload: dict[str, Any]) -> None:
        if not self.is_complete and self.state != State.ACCESS_METHODS:
            raise ShipError(f"cannot send data in state {self.state.value}")
        await self.transport.send(model.data(payload))

    async def close(self, reason: str = "unspecific") -> None:
        """Orderly close (SHIP 13.4.7: announce, then close the websocket)."""
        if self._closed.is_set():
            return
        if self.state in (State.ACCESS_METHODS, State.COMPLETE):
            with contextlib.suppress(Exception):
                await self.transport.send(model.connection_close("announce", reason, 500))
            await asyncio.sleep(0.1)
        await self._shutdown(State.CLOSED)

    @property
    def on_data(self) -> DataHandler | None:
        return self._on_data

    @on_data.setter
    def on_data(self, handler: DataHandler | None) -> None:
        """Set the SPINE payload handler; payloads that arrived earlier are replayed."""
        self._on_data = handler
        early, self._early_data = self._early_data, []
        for payload in early if handler else []:
            try:
                result = handler(payload)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("SPINE handler failed")

    async def wait_closed(self) -> None:
        await self._closed.wait()

    # --- internals -----------------------------------------------------------

    def _set_state(self, state: State, error: Exception | None = None) -> None:
        if state == self.state and error is None:
            return
        self.state = state
        self.error = error
        _LOGGER.debug("%s: SHIP state %s%s", self.remote_ski[:8], state.value,
                      f" ({error})" if error else "")
        if self._on_state:
            try:
                self._on_state(self, state, error)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("state callback failed")

    async def _fail(self, err: Exception) -> None:
        _LOGGER.debug("%s: SHIP handshake failed: %s", self.remote_ski[:8], err)
        await self._shutdown(State.ERROR, err)

    async def _shutdown(self, state: State, error: Exception | None = None) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._set_state(state, error)
        with contextlib.suppress(Exception):
            await self.transport.close(4001, "close")
        if self._reader and self._reader is not asyncio.current_task():
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader

    async def _read_loop(self) -> None:
        try:
            while True:
                message = await self.transport.receive()
                if not message:
                    continue
                if message[0] == MsgType.END:
                    await self._handle_close_message(message)
                    continue
                if message[0] == MsgType.DATA:
                    await self._handle_data(message)
                    continue
                if self.state in (State.ACCESS_METHODS, State.COMPLETE) and message[0] == MsgType.CONTROL:
                    try:
                        await self._handle_data_exchange_control(message)
                    except ShipError as err:
                        await self._shutdown(State.ERROR, err)
                        return
                    continue
                await self._inbox.put(message)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - connection gone
            await self._inbox.put(err)
            if self.state == State.COMPLETE:
                await self._shutdown(State.CLOSED, err if not isinstance(err, ConnectionError) else None)

    async def _next(self, timeout: float) -> bytes:
        try:
            item = await asyncio.wait_for(self._inbox.get(), timeout)
        except TimeoutError as err:
            raise ShipError(f"timeout in state {self.state.value}") from err
        if isinstance(item, Exception):
            if getattr(item, "code", None) == model.CLOSE_REJECTED:
                raise RemoteAbortError("remote rejected this node (not paired yet)") from item
            raise ShipError(f"connection lost in state {self.state.value}: {item}") from item
        return item

    async def _next_json(self, timeout: float) -> dict[str, Any]:
        message = await self._next(timeout)
        kind, body = model.parse(message)
        if kind != MsgType.CONTROL or body is None:
            raise ShipError(f"unexpected SHIP message type {kind} in state {self.state.value}")
        return body

    # CMI: connection mode initialisation (SHIP 13.4.3)
    async def _cmi(self) -> None:
        self._set_state(State.CMI)
        if self.role == Role.CLIENT:
            await self.transport.send(model.INIT_MESSAGE)
            message = await self._next(model.CMI_TIMEOUT)
            self._check_init(message)
        else:
            message = await self._next(model.CMI_TIMEOUT)
            try:
                self._check_init(message)
            finally:
                # SHIP: the server answers with init even when rejecting.
                await self.transport.send(model.INIT_MESSAGE)

    @staticmethod
    def _check_init(message: bytes) -> None:
        if message[0] != MsgType.INIT or (len(message) > 1 and message[1] != 0):
            raise ShipError(f"invalid CMI message {message[:4]!r}")

    # Hello (SHIP 13.4.4.1). We only connect to/accept trusted peers, so we are
    # always "ready"; the remote may still be "pending" until a user trusts us.
    async def _hello(self) -> None:
        self._set_state(State.HELLO)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + model.HELLO_INIT_TIMEOUT
        accepted_prolongations = 0
        await self.transport.send(model.hello("ready", int(model.HELLO_INIT_TIMEOUT * 1000)))
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                await self._send_quiet(model.hello("aborted"))
                raise ShipError("hello timeout: remote did not become ready (not trusted?)")
            body = await self._next_json(remaining)
            hello = body.get("connectionHello")
            if not isinstance(hello, dict):
                await self._send_quiet(model.hello("aborted"))
                raise ShipError(f"expected connectionHello, got {list(body)}")
            phase = hello.get("phase")
            if phase == "ready":
                return
            if phase == "aborted":
                raise RemoteAbortError("remote aborted hello (it does not trust this node)")
            if phase == "pending":
                self._set_state(State.HELLO_WAITING_FOR_TRUST)
                if hello.get("prolongationRequest"):
                    if (accepted_prolongations < model.HELLO_PROLONGATIONS_ALWAYS_ACCEPTED
                            or self._allow_waiting_for_trust()):
                        accepted_prolongations += 1
                        deadline += model.HELLO_INC_TIMEOUT
                    waiting = max(int((deadline - loop.time()) * 1000), 0)
                    await self.transport.send(model.hello("ready", waiting))
                continue
            await self._send_quiet(model.hello("aborted"))
            raise ShipError(f"unexpected hello phase {phase!r}")

    # Protocol handshake (SHIP 13.4.4.2)
    async def _protocol(self) -> None:
        self._set_state(State.PROTOCOL)
        if self.role == Role.CLIENT:
            await self.transport.send(model.protocol_handshake("announceMax"))
            choice = await self._protocol_message("select")
            version = choice.get("version", {})
            formats = (choice.get("formats") or {}).get("format") or []
            if isinstance(formats, str):
                formats = [formats]
            if version.get("major") != 1 or version.get("minor") != 0 or formats != ["JSON-UTF8"]:
                await self._send_quiet(model.protocol_handshake_error(3))
                raise ShipError(f"unsupported protocol selection {choice}")
            await self.transport.send(model.protocol_handshake("select"))
        else:
            await self._protocol_message("announceMax")
            await self.transport.send(model.protocol_handshake("select"))
            await self._protocol_message("select")

    async def _protocol_message(self, expected: str) -> dict[str, Any]:
        body = await self._next_json(model.PROTOCOL_TIMEOUT)
        handshake = body.get("messageProtocolHandshake")
        if not isinstance(handshake, dict) or handshake.get("handshakeType") != expected:
            await self._send_quiet(model.protocol_handshake_error(2))
            raise ShipError(f"expected protocol handshake {expected}, got {body}")
        return handshake

    # PIN verification (SHIP 13.4.4.3): only "none" is supported, like ship-go.
    async def _pin(self) -> None:
        self._set_state(State.PIN)
        await self.transport.send(model.pin_state_none())
        body = await self._next_json(model.PIN_TIMEOUT)
        state = (body.get("connectionPinState") or {}).get("pinState")
        if state != "none":
            raise ShipError(f"unsupported PIN state {state!r}")

    # Access methods (SHIP 13.4.6): from here on data may already flow.
    async def _access_methods(self) -> None:
        self._set_state(State.ACCESS_METHODS)
        # Messages that arrived during the PIN phase but belong here.
        while not self._inbox.empty():
            item = self._inbox.get_nowait()
            if isinstance(item, Exception):
                raise ShipError(f"connection lost: {item}") from item
            await self._handle_data_exchange_control(item)
        await self.transport.send(model.access_methods_request())
        done = asyncio.ensure_future(self._access_methods_done.wait())
        closed = asyncio.ensure_future(self._closed.wait())
        try:
            await asyncio.wait({done, closed}, timeout=model.ACCESS_METHODS_TIMEOUT,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            done.cancel()
            closed.cancel()
        if self._closed.is_set():
            raise self.error if isinstance(self.error, ShipError) else ShipError("connection closed")
        if not self._access_methods_done.is_set():
            raise ShipError("no accessMethods answer from remote")

    async def _handle_data_exchange_control(self, message: bytes) -> None:
        try:
            _, body = model.parse(message)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("%s: ignoring unparsable control message", self.remote_ski[:8])
            return
        body = body or {}
        if "accessMethodsRequest" in body:
            await self.transport.send(model.access_methods(self.local_ship_id))
        elif "accessMethods" in body:
            methods = body["accessMethods"] or {}
            if "id" not in methods:  # like ship-go: the id must be present (may be empty)
                raise ShipError("accessMethods without SHIP id")
            remote_id = methods["id"] or ""
            if self.remote_ship_id and remote_id and remote_id != self.remote_ship_id:
                raise ShipError(f"SHIP id mismatch: expected {self.remote_ship_id}, got {remote_id}")
            self.remote_ship_id = self.remote_ship_id or remote_id
            self._access_methods_done.set()
        else:
            _LOGGER.debug("%s: ignoring control message %s", self.remote_ski[:8], list(body))

    async def _handle_data(self, message: bytes) -> None:
        if self.state not in (State.ACCESS_METHODS, State.COMPLETE):
            _LOGGER.debug("%s: dropping data outside data exchange", self.remote_ski[:8])
            return
        try:
            _, body = model.parse(message)
            payload = (body or {}).get("data", {}).get("payload")
        except Exception:  # noqa: BLE001
            _LOGGER.debug("%s: unparsable data message", self.remote_ski[:8])
            return
        if not isinstance(payload, dict):
            return
        if self._on_data is None:
            if len(self._early_data) < 100:
                self._early_data.append(payload)
            return
        try:
            result = self._on_data(payload)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001
            _LOGGER.exception("SPINE handler failed")

    async def _handle_close_message(self, message: bytes) -> None:
        try:
            _, body = model.parse(message)
        except Exception:  # noqa: BLE001
            return
        phase = ((body or {}).get("connectionClose") or {}).get("phase")
        if phase == "announce":
            await self._send_quiet(model.connection_close("confirm"))
            await asyncio.sleep(0.5)
        await self._shutdown(State.CLOSED)

    async def _send_quiet(self, message: bytes) -> None:
        with contextlib.suppress(Exception):
            await self.transport.send(message)

