"""mDNS announcement records (no network needed)."""

from __future__ import annotations

import socket

from pyeebus.ship import mdns


class FakeZeroconf:
    def __init__(self) -> None:
        self.registered = []

    async def async_register_service(self, info, allow_name_change=False):
        self.registered.append(info)

    async def async_unregister_service(self, info):
        pass


async def test_announcement_carries_its_own_host_name_and_addresses(monkeypatch):
    monkeypatch.setenv("EEBUS_ANNOUNCE_IP", "192.168.36.50")
    zc = FakeZeroconf()
    m = mdns.ShipMdns(zc)
    await m.announce(instance="elli-eebus-proxy-266c3dcb", ship_id="x", ski="ab" * 20, port=4712,
                     brand="b", model="m", device_type="EnergyManagementSystem")
    info = zc.registered[0]
    assert info.server == "elli-eebus-proxy-266c3dcb.local."
    assert info.addresses == [socket.inet_aton("192.168.36.50")]
    assert info.properties[b"ski"] == ("ab" * 20).encode()


def test_local_addresses_skip_loopback(monkeypatch):
    monkeypatch.delenv("EEBUS_ANNOUNCE_IP", raising=False)
    assert all(not a.startswith("127.") for a in mdns.local_addresses())
