"""EEBUS service: a SHIP node plus the local SPINE device (port of eebus-go's service)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .ship import Identity, ShipNode, TrustStore
from .ship.connection import ShipConnection
from .spine.device import LocalDevice, LocalEntity, RemoteDevice
from .spine.model import device_address

_LOGGER = logging.getLogger(__name__)


class EebusService:
    """Runs SHIP (discovery, pairing, connections) and the SPINE device on top.

    ``entity_types`` are created as local entities ``[1]``, ``[2]``, ... (entity
    ``[0]`` is the device information entity).
    """

    def __init__(
        self,
        identity: Identity,
        *,
        brand: str,
        model: str,
        serial: str,
        device_type: str = "EnergyManagementSystem",
        entity_types: tuple[str, ...] = ("CEM",),
        vendor: str | None = None,
        ship_id: str | None = None,
        port: int = 4712,
        trust: TrustStore | None = None,
        heartbeat_timeout: float = 4.0,
        **node_options: Any,
    ) -> None:
        self.identity = identity
        self.ship_id = ship_id or f"{brand}-{model}-{serial}".replace(" ", "-")
        self.device = LocalDevice(
            device_address(brand, model, serial, vendor), device_type, brand=brand, model=model,
            serial=serial, ship_id=self.ship_id, heartbeat_timeout=heartbeat_timeout)
        self.entities: list[LocalEntity] = [self.device.add_entity(t) for t in entity_types]
        self.node = ShipNode(identity, ship_id=self.ship_id, brand=brand, model=model,
                             device_type=device_type, serial=serial, port=port, trust=trust,
                             **node_options)
        self.node.on_connected = self._on_connected
        self.node.on_disconnected = self._on_disconnected
        self._writers: dict[str, asyncio.Task] = {}
        self.trace: Callable[[str, str, dict[str, Any]], None] | None = None
        """Optional hook for SPINE traffic: (ski, "in" or "out", payload)."""

    @classmethod
    def from_directory(cls, directory: str | Path, **kwargs: Any) -> EebusService:
        """Keep certificate, key and trusted peers in ``directory``."""
        directory = Path(directory)
        identity = Identity.load_or_create(directory, kwargs.get("model", "pyeebus"))
        trust = TrustStore.load(directory / "trust.json")
        return cls(identity, trust=trust, **kwargs)

    @property
    def ski(self) -> str:
        return self.identity.ski

    @property
    def remote_devices(self) -> dict[str, RemoteDevice]:
        return self.device.remote_devices

    async def start(self) -> None:
        self.device.start()
        await self.node.start()

    async def stop(self) -> None:
        self.device.stop()
        await self.node.stop()
        for task in self._writers.values():
            task.cancel()

    def trust(self, ski: str, cert_pem: bytes | None = None) -> None:
        """Pair with a remote device (the remote side has to trust us as well)."""
        self.node.trust_ski(ski, cert_pem)

    async def untrust(self, ski: str) -> None:
        await self.node.untrust_ski(ski)

    # SHIP <-> SPINE
    def _on_connected(self, conn: ShipConnection) -> None:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        ski = conn.remote_ski

        def send(payload: dict[str, Any]) -> None:
            if self.trace:
                self.trace(ski, "out", payload)
            queue.put_nowait(payload)

        remote = self.device.setup_remote(ski, send)

        def receive(payload: dict[str, Any]) -> None:
            if self.trace:
                self.trace(ski, "in", payload)
            remote.handle_payload(payload)

        conn.on_data = receive
        self._writers[conn.remote_ski] = asyncio.create_task(self._writer(conn, queue))

    async def _writer(self, conn: ShipConnection, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                payload = await queue.get()
                try:
                    await conn.send_spine(payload)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("sending to %s failed: %s", conn.remote_ski[:8], err)
                    return

    def _on_disconnected(self, ski: str) -> None:
        if (task := self._writers.pop(ski, None)) is not None:
            task.cancel()
        self.device.remove_remote(ski)
