"""SHIP service announcement and discovery via mDNS (SHIP 7)."""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass, field

from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from .cert import normalize_ski
from .model import SERVICE_TYPE, WEBSOCKET_PATH

_LOGGER = logging.getLogger(__name__)


@dataclass
class ShipService:
    """A SHIP node found on the network."""

    name: str
    ski: str
    ship_id: str
    host: str
    addresses: list[str]
    port: int
    path: str = WEBSOCKET_PATH
    register: bool = False  # remote accepts new nodes automatically
    brand: str = ""
    model: str = ""
    device_type: str = ""
    serial: str = ""
    extra: dict[str, str] = field(default_factory=dict)


def _txt(props: dict[bytes, bytes | None]) -> dict[str, str]:
    return {k.decode(errors="replace").lower(): (v or b"").decode(errors="replace")
            for k, v in props.items()}


def service_from_info(info: AsyncServiceInfo) -> ShipService | None:
    txt = _txt(info.properties)
    if txt.get("txtvers") != "1" or not all(k in txt for k in ("id", "ski", "register")):
        return None  # SHIP 7.3.2: ignore invalid announcements
    addresses = info.parsed_scoped_addresses(IPVersion.All)
    return ShipService(
        name=info.name,
        ski=normalize_ski(txt["ski"]),
        ship_id=txt["id"],
        host=(info.server or "").rstrip("."),
        addresses=addresses,
        port=info.port or 0,
        path=txt.get("path") or WEBSOCKET_PATH,
        register=txt.get("register", "false").lower() == "true",
        brand=txt.get("brand", ""),
        model=txt.get("model", ""),
        device_type=txt.get("type", ""),
        serial=txt.get("serial", ""),
        extra={k: v for k, v in txt.items()
               if k not in {"txtvers", "id", "ski", "register", "path", "brand", "model",
                            "type", "serial"}},
    )


class ShipMdns:
    """Announce this node and track other SHIP nodes."""

    def __init__(self, zeroconf: AsyncZeroconf | None = None) -> None:
        self._azc = zeroconf
        self._own_zc = zeroconf is None
        self._info: AsyncServiceInfo | None = None
        self._browser: AsyncServiceBrowser | None = None
        self.services: dict[str, ShipService] = {}  # by SKI
        self._names: dict[str, str] = {}  # mDNS name -> SKI
        self._listeners: list[Callable[[ShipService, bool], None]] = []
        self._tasks: set[asyncio.Task] = set()

    @property
    def zeroconf(self) -> AsyncZeroconf:
        if self._azc is None:
            self._azc = AsyncZeroconf()
        return self._azc

    def add_listener(self, callback: Callable[[ShipService, bool], None]) -> None:
        """callback(service, added) for every found (True) or removed (False) node."""
        self._listeners.append(callback)

    async def announce(self, *, instance: str, ship_id: str, ski: str, port: int,
                       brand: str, model: str, device_type: str, serial: str = "",
                       register: bool = False) -> None:
        props = {
            "txtvers": "1",
            "path": WEBSOCKET_PATH,
            "id": ship_id,
            "ski": ski,
            "brand": brand,
            "model": model,
            "type": device_type,
            "register": "true" if register else "false",
        }
        if serial:
            props["serial"] = serial
        hostname = socket.gethostname().split(".")[0] or "pyeebus"
        self._info = AsyncServiceInfo(
            SERVICE_TYPE, f"{instance}.{SERVICE_TYPE}", port=port, properties=props,
            server=f"{hostname}.local.",
        )
        await self.zeroconf.async_register_service(self._info, allow_name_change=True)

    async def browse(self) -> None:
        self._browser = AsyncServiceBrowser(
            self.zeroconf.zeroconf, [SERVICE_TYPE], handlers=[self._on_change])

    def _on_change(self, zeroconf, service_type: str, name: str,
                   state_change: ServiceStateChange) -> None:
        task = asyncio.ensure_future(self._handle(service_type, name, state_change))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, service_type: str, name: str, change: ServiceStateChange) -> None:
        if change is ServiceStateChange.Removed:
            ski = self._names.pop(name, None)
            if ski and (service := self.services.pop(ski, None)):
                self._notify(service, False)
            return
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(self.zeroconf.zeroconf, 3000):
            return
        service = service_from_info(info)
        if service is None or (self._info and service.name == self._info.name):
            return
        self._names[name] = service.ski
        self.services[service.ski] = service
        self._notify(service, True)

    def _notify(self, service: ShipService, added: bool) -> None:
        for callback in self._listeners:
            try:
                callback(service, added)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("mDNS listener failed")

    async def close(self) -> None:
        if self._browser:
            await self._browser.async_cancel()
        if self._info:
            await self.zeroconf.async_unregister_service(self._info)
        for task in list(self._tasks):
            task.cancel()
        if self._own_zc and self._azc is not None:
            await self._azc.async_close()
