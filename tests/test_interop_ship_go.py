"""Interop against ship-go (the reference implementation).

Needs a built harness (see interop/ship-go-harness); set SHIP_GO_HARNESS to the
binary. Skipped otherwise.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from pyeebus.ship import Identity, ShipNode, State

HARNESS = os.environ.get("SHIP_GO_HARNESS")
pytestmark = pytest.mark.skipif(not HARNESS, reason="SHIP_GO_HARNESS not set")


async def _start_harness(*args: str) -> tuple[asyncio.subprocess.Process, dict[str, str]]:
    proc = await asyncio.create_subprocess_exec(
        HARNESS, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    info: dict[str, str] = {}
    while True:
        line = (await asyncio.wait_for(proc.stdout.readline(), 10)).decode().strip()
        if line.startswith("SKI "):
            info["ski"] = line.split()[1]
        if line == "READY":
            return proc, info


async def _wait_for(proc, prefix: str, timeout: float = 15) -> str:
    async def read():
        while True:
            line = (await proc.stdout.readline()).decode().strip()
            if not line and proc.stdout.at_eof():
                raise EOFError
            if line.startswith(prefix):
                return line
    return await asyncio.wait_for(read(), timeout)


def _node() -> ShipNode:
    return ShipNode(Identity.create("py"), ship_id="py-ship-id", brand="pyeebus", model="test",
                    device_type="EnergyManagementSystem", port=0, announce=False,
                    discover=False, host="127.0.0.1")


async def test_python_client_to_ship_go_server(unused_tcp_port):
    node = _node()
    await node.start()
    proc, info = await _start_harness("-port", str(unused_tcp_port), "-trust", node.ski)
    try:
        node.trust.trust(info["ski"])
        conn = await node.connect("127.0.0.1", unused_tcp_port, info["ski"])
        assert conn.state == State.COMPLETE
        assert conn.remote_ship_id is not None
        got: asyncio.Queue = asyncio.Queue()
        conn.on_data = got.put_nowait
        payload = {"datagram": {"header": {"specificationVersion": "1.3.0"},
                                "payload": {"cmd": [{"function": "x"}]}}}
        await conn.send_spine(payload)
        assert (await asyncio.wait_for(got.get(), 10)) == payload
        await conn.close()
    finally:
        proc.terminate()
        await proc.wait()
        await node.stop()


async def test_ship_go_client_to_python_server(unused_tcp_port, tmp_path: Path):
    node = _node()
    cert_file = tmp_path / "go.pem"
    # Start harness first to learn its SKI and certificate, then our server.
    await node.start()
    proc, info = await _start_harness(
        "-port", str(unused_tcp_port), "-trust", node.ski,
        "-dial", f"127.0.0.1:{node.port}", "-cert-out", str(cert_file))
    try:
        connected = asyncio.Event()
        got: asyncio.Queue = asyncio.Queue()

        def on_connected(conn):
            conn.on_data = got.put_nowait
            connected.set()

        node.on_connected = on_connected
        node._remember_cert(info["ski"], _pem_to_der(cert_file.read_bytes()))  # noqa: SLF001
        await asyncio.wait_for(connected.wait(), 30)
        conn = node.connections[info["ski"]]
        assert conn.remote_ship_id is not None
        payload = {"datagram": {"payload": {"cmd": [{"y": 1}]}}}
        await conn.send_spine(payload)
        assert (await asyncio.wait_for(got.get(), 10)) == payload
    finally:
        proc.terminate()
        await proc.wait()
        await node.stop()


def _pem_to_der(pem: bytes) -> bytes:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    return x509.load_pem_x509_certificate(pem).public_bytes(serialization.Encoding.DER)


@pytest.fixture
def unused_tcp_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
