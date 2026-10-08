"""SHIP node: server, client, mDNS and trust handling (port of ship-go's hub)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from . import transport
from .cert import Identity, normalize_ski, ski_from_certificate
from .connection import ShipConnection
from .mdns import ShipMdns, ShipService
from .model import DEFAULT_PORT, WEBSOCKET_PATH, Role, ShipError, State

_LOGGER = logging.getLogger(__name__)

RETRY_MIN = 2.0
RETRY_MAX = 120.0


def _der_to_pem(der: bytes) -> bytes:
    return x509.load_der_x509_certificate(der).public_bytes(serialization.Encoding.PEM)


@dataclass
class TrustStore:
    """Trusted remote SKIs, optionally with their certificate (needed to accept
    incoming connections). Persisted as JSON when a path is given."""

    path: Path | None = None
    peers: dict[str, str | None] = field(default_factory=dict)  # ski -> cert PEM

    @classmethod
    def load(cls, path: str | Path) -> TrustStore:
        path = Path(path)
        peers = json.loads(path.read_text()) if path.exists() else {}
        return cls(path, peers)

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.peers, indent=2))

    def trust(self, ski: str, cert_pem: bytes | None = None) -> None:
        ski = normalize_ski(ski)
        if cert_pem is not None or ski not in self.peers:
            self.peers[ski] = cert_pem.decode() if cert_pem else self.peers.get(ski)
            self.save()

    def remove(self, ski: str) -> None:
        self.peers.pop(normalize_ski(ski), None)
        self.save()

    def is_trusted(self, ski: str) -> bool:
        return normalize_ski(ski) in self.peers

    def certificates(self) -> list[bytes]:
        return [pem.encode() for pem in self.peers.values() if pem]


ConnectionCallback = Callable[[ShipConnection], Awaitable[None] | None]


class ShipNode:
    """A SHIP node that accepts and opens connections to trusted peers."""

    def __init__(
        self,
        identity: Identity,
        *,
        ship_id: str,
        brand: str,
        model: str,
        device_type: str,
        serial: str = "",
        port: int = DEFAULT_PORT,
        trust: TrustStore | None = None,
        zeroconf=None,
        announce: bool = True,
        discover: bool = True,
        auto_connect: bool = True,
        host: str | None = None,
    ) -> None:
        self.identity = identity
        self.ski = identity.ski
        self.ship_id = ship_id
        self.brand, self.model, self.device_type, self.serial = brand, model, device_type, serial
        self.port = port
        self.host = host
        self.trust = trust or TrustStore()
        self.announce_enabled, self.discover_enabled = announce, discover
        self.auto_connect = auto_connect
        self.mdns = ShipMdns(zeroconf) if (announce or discover) else None
        self.connections: dict[str, ShipConnection] = {}
        self.on_connected: ConnectionCallback | None = None
        self.on_disconnected: Callable[[str], None] | None = None
        self.on_state: Callable[[str, State, Exception | None], None] | None = None
        self._server_ctx = None
        self._runner: web.AppRunner | None = None
        self._dialing: dict[str, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        self._stopping = False

    # --- lifecycle ---------------------------------------------------------------

    async def start(self) -> None:
        self._server_ctx = transport.server_context(self.identity, self.trust.certificates())
        app = web.Application()
        app.router.add_get(WEBSOCKET_PATH, self._handle_incoming)
        app.router.add_get("/", self._handle_incoming)
        self._runner = web.AppRunner(app, handle_signals=False, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port, ssl_context=self._server_ctx)
        await site.start()
        if self.port == 0:
            self.port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        if self.mdns:
            if self.announce_enabled:
                await self.mdns.announce(
                    instance=f"{self.brand}-{self.model}-{self.ski[:8]}".replace(" ", "-"),
                    ship_id=self.ship_id, ski=self.ski, port=self.port, brand=self.brand,
                    model=self.model, device_type=self.device_type, serial=self.serial,
                    register=False)
            if self.discover_enabled:
                self.mdns.add_listener(self._on_service)
                await self.mdns.browse()

    async def stop(self) -> None:
        self._stopping = True
        for task in list(self._dialing.values()) + list(self._tasks):
            task.cancel()
        for conn in list(self.connections.values()):
            await conn.close("unspecific")
        if self.mdns:
            await self.mdns.close()
        if self._runner:
            await self._runner.cleanup()

    # --- trust ---------------------------------------------------------------------

    def trust_ski(self, ski: str) -> None:
        """Trust a remote node and (if discovered) connect to it."""
        ski = normalize_ski(ski)
        self.trust.trust(ski)
        if self.mdns and ski in self.mdns.services:
            self._schedule_connect(self.mdns.services[ski])

    async def untrust_ski(self, ski: str) -> None:
        ski = normalize_ski(ski)
        self.trust.remove(ski)
        if (conn := self.connections.get(ski)) is not None:
            await conn.close("removedConnection")

    def _remember_cert(self, ski: str, der: bytes) -> None:
        pem = _der_to_pem(der)
        self.trust.trust(ski, pem)
        if self._server_ctx is not None:
            transport.add_trusted_cert(self._server_ctx, pem)

    # --- outgoing --------------------------------------------------------------------

    async def connect(self, host: str, port: int, ski: str,
                      path: str = WEBSOCKET_PATH) -> ShipConnection:
        """Connect to a trusted node and complete the SHIP handshake."""
        ski = normalize_ski(ski)
        if not self.trust.is_trusted(ski):
            raise ShipError(f"SKI {ski} is not trusted")
        ws, der = await transport.connect(host, port, path, self.identity)
        try:
            remote_ski = ski_from_certificate(der)
        except Exception:
            await ws.close()
            raise
        if remote_ski != ski:
            await ws.close()
            raise ShipError(f"expected SKI {ski}, remote presented {remote_ski}")
        self._remember_cert(ski, der)
        conn = self._make_connection(ws, Role.CLIENT, ski)
        await self._run(conn)
        return conn

    def _on_service(self, service: ShipService, added: bool) -> None:
        if added and self.auto_connect and self.trust.is_trusted(service.ski):
            self._schedule_connect(service)

    def _schedule_connect(self, service: ShipService) -> None:
        if self._stopping or service.ski in self._dialing or service.ski in self.connections:
            return
        task = asyncio.create_task(self._dial_loop(service), name=f"ship-dial-{service.ski[:8]}")
        self._dialing[service.ski] = task
        task.add_done_callback(lambda _t, ski=service.ski: self._dialing.pop(ski, None))

    async def _dial_loop(self, service: ShipService) -> None:
        delay = RETRY_MIN
        while not self._stopping and self.trust.is_trusted(service.ski):
            if service.ski in self.connections:
                return
            hosts = [*service.addresses, service.host] if service.addresses else [service.host]
            for host in hosts:
                if not host:
                    continue
                try:
                    conn = await self.connect(host, service.port, service.ski, service.path)
                    await conn.wait_closed()
                    delay = RETRY_MIN  # connection ended; reconnect soon
                    break
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("connect to %s at %s failed: %s", service.ski[:8], host, err)
            if self.mdns and service.ski in self.mdns.services:
                service = self.mdns.services[service.ski]
            await asyncio.sleep(delay * random.uniform(0.8, 1.2))
            delay = min(delay * 2, RETRY_MAX)

    # --- incoming -----------------------------------------------------------------

    async def _handle_incoming(self, request: web.Request) -> web.StreamResponse:
        try:
            ws, tr, ski = await transport.accept(request)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("rejected incoming SHIP connection: %s", err)
            raise web.HTTPForbidden() from err
        if not self.trust.is_trusted(ski):
            await tr.close(4001, "not trusted")
            return ws
        conn = self._make_connection(tr, Role.SERVER, ski)
        with contextlib.suppress(Exception):
            await self._run(conn)
            await conn.wait_closed()
        return ws

    # --- shared -------------------------------------------------------------------

    def _make_connection(self, tr, role: Role, ski: str) -> ShipConnection:
        def on_state(_conn: ShipConnection, state: State, err: Exception | None) -> None:
            if self.on_state:
                self.on_state(ski, state, err)
        return ShipConnection(tr, role, self.ship_id, ski, on_state=on_state)

    async def _run(self, conn: ShipConnection) -> None:
        await conn.run()
        ski = conn.remote_ski
        existing = self.connections.get(ski)
        if existing is not None and not existing._closed.is_set():  # noqa: SLF001
            # SHIP 12.2.2 double connection: both sides keep the connection that
            # was opened by the node with the higher SKI.
            initiator_new = self.ski if conn.role == Role.CLIENT else ski
            if initiator_new < max(self.ski, ski):
                await conn.close()
                raise ShipError("double connection, keeping the existing one")
            await existing.close()
        self.connections[ski] = conn
        task = asyncio.create_task(self._watch(conn))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if self.on_connected:
            result = self.on_connected(conn)
            if asyncio.iscoroutine(result):
                await result

    async def _watch(self, conn: ShipConnection) -> None:
        await conn.wait_closed()
        if self.connections.get(conn.remote_ski) is conn:
            del self.connections[conn.remote_ski]
            if self.on_disconnected:
                self.on_disconnected(conn.remote_ski)
        if (not self._stopping and self.auto_connect and self.mdns
                and conn.remote_ski in self.mdns.services and self.trust.is_trusted(conn.remote_ski)):
            self._schedule_connect(self.mdns.services[conn.remote_ski])
