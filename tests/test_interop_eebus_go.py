"""SPINE and use case interop with eebus-go / spine-go.

Runs only when EEBUS_GO_HARNESS points to the built interop/eebus-go-harness.
"""

from __future__ import annotations

import asyncio
import os
import socket

import pytest

from pyeebus.service import EebusService
from pyeebus.ship import Identity
from pyeebus.simulator import SimulatedEVSE
from pyeebus.usecases import EVCC, EVCEM, EVSECC, OPEV, DataNotAvailable, PhaseLimit

HARNESS = os.environ.get("EEBUS_GO_HARNESS")
pytestmark = pytest.mark.skipif(not HARNESS, reason="EEBUS_GO_HARNESS not set")
NODE = {"announce": False, "discover": False, "host": "127.0.0.1"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Harness:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.lines: list[str] = []
        self._task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        while line := await self.proc.stdout.readline():
            self.lines.append(line.decode().strip())

    async def expect(self, prefix: str, timeout: float = 10.0) -> str:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            for line in self.lines:
                if line.startswith(prefix):
                    return line
            await asyncio.sleep(0.02)
        raise AssertionError(f"no {prefix!r} line; got {self.lines}")

    async def send(self, command: str) -> None:
        self.proc.stdin.write(command.encode() + b"\n")
        await self.proc.stdin.drain()

    async def stop(self) -> None:
        if self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        self._task.cancel()


async def start_harness(mode: str, trust: str, port: int) -> Harness:
    proc = await asyncio.create_subprocess_exec(
        HARNESS, "-mode", mode, "-port", str(port), "-trust", trust,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    h = Harness(proc)
    await h.expect("READY")
    return h


async def wait_for(condition, timeout: float = 10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        try:
            if result := condition():
                return result
        except DataNotAvailable:
            pass
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


async def test_eebus_go_cem_controls_pyeebus_evse():
    """eebus-go CEM (EVSECC, EVCC, EVCEM, OPEV) <- pyeebus simulated EVSE."""
    evse = SimulatedEVSE(Identity.create("evse"), port=0, **NODE)
    port = free_port()
    harness = await start_harness("cem", evse.service.ski, port)
    try:
        go_ski = (await harness.expect("SKI")).split()[1]
        evse.service.node.trust.trust(go_ski)
        await evse.start()
        await evse.service.node.connect("127.0.0.1", port, go_ski)
        assert await harness.expect("EVSE_MANUFACTURER") == "EVSE_MANUFACTURER SimEVSE"

        evse.plug_in()
        assert await harness.expect("OPEV_LIMITS") == "OPEV_LIMITS 16,16,16"
        assert await harness.expect("WRITE_RESULT") == "WRITE_RESULT 0"
        await wait_for(lambda: evse.limits["obligation"] == [10, 10, 10])
        assert await harness.expect("EVCC_STANDARD") == "EVCC_STANDARD iec61851"

        evse.set_charging(10, 1234)
        await harness.expect("EVCEM_CURRENT 10,10,10")
        await harness.expect("EVCEM_ENERGY 1234")

        evse.unplug()
        await harness.expect("EVENT cem-evcc-EvDisconnected")
    finally:
        await evse.stop()
        await harness.stop()


async def test_pyeebus_cem_controls_spine_go_evse():
    """pyeebus CEM -> spine-go EVSE with an EV entity."""
    cem = EebusService(Identity.create("cem"), brand="pyeebus", model="TestCEM", serial="1", port=0, **NODE)
    ucs = {cls.__name__: cls(cem.entities[0]).setup() for cls in (EVSECC, EVCC, EVCEM, OPEV)}
    port = free_port()
    harness = await start_harness("evse", cem.ski, port)
    try:
        go_ski = (await harness.expect("SKI")).split()[1]
        cem.trust(go_ski)
        await cem.start()
        await cem.node.connect("127.0.0.1", port, go_ski)
        remote = await wait_for(lambda: next(iter(cem.remote_devices.values()), None))
        evse_entity = await wait_for(lambda: next((e for e in remote.entities if e.type == "EVSE"), None))
        manufacturer = await wait_for(lambda: ucs["EVSECC"].manufacturer_data(evse_entity))
        assert manufacturer["deviceName"] == "GoEVSE"
        await harness.expect("SUBSCRIBED NodeManagement")

        await harness.send("plug")
        ev = await wait_for(lambda: remote.entity((1, 1)))
        limits = await wait_for(lambda: ucs["OPEV"].load_control_limits(ev))
        assert [lim.value for lim in limits] == [16, 16, 16]
        assert await wait_for(lambda: ucs["EVCC"].communication_standard(ev) == "iso15118-2ed1")
        await harness.expect("BOUND LoadControl")

        await ucs["OPEV"].write_load_control_limits(ev, [PhaseLimit(p, 10) for p in "abc"])
        assert await harness.expect("LIMIT_WRITE 3") == "LIMIT_WRITE 3 10 true"
        await wait_for(lambda: [lim.value for lim in ucs["OPEV"].load_control_limits(ev)] == [10, 10, 10])

        await harness.send("measure 8 500")
        assert await wait_for(lambda: ucs["EVCEM"].current_per_phase(ev) == [8, 8, 8])
        assert ucs["EVCEM"].energy_charged(ev) == 500

        await harness.send("unplug")
        await wait_for(lambda: remote.entity((1, 1)) is None)
    finally:
        await cem.stop()
        await harness.stop()
