"""pyeebus command line: discover EEBUS devices, connect as an energy manager, simulate an EVSE."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from .service import EebusService
from .ship import Identity, RemoteAbortError, State, TrustStore, is_ski_valid, normalize_ski
from .ship.mdns import ShipMdns, ShipService
from .spine import Change, Event, EventType, RemoteDevice, RemoteEntity, Role, spawn
from .usecases import (
    ALL_EV_USE_CASES,
    EVCC,
    EVCEM,
    EVSECC,
    LPC,
    MPC,
    OPEV,
    OSCEV,
    DataNotAvailable,
    LoadLimit,
    UseCase,
)

CONFIG_DIR = Path.home() / ".config" / "pyeebus"


def _print_service(s: ShipService) -> None:
    print(f"- {s.brand} {s.model} ({s.device_type})".strip())
    print(f"    SKI      {s.ski}")
    print(f"    SHIP id  {s.ship_id}")
    print(f"    address  {', '.join(s.addresses) or s.host}:{s.port}{s.path}")
    print(f"    register {s.register}")


async def discover(seconds: float) -> int:
    mdns = ShipMdns()
    await mdns.browse()
    print(f"Looking for EEBUS devices for {seconds:g} s ...")
    await asyncio.sleep(seconds)
    await mdns.close()
    if not mdns.services:
        print("No EEBUS (SHIP) devices found. Same network? mDNS allowed?")
        return 1
    for service in mdns.services.values():
        _print_service(service)
    return 0


# --- connect -------------------------------------------------------------------------------


def _entity_line(entity: RemoteEntity) -> str:
    addr = ".".join(str(e) for e in entity.entity)
    features = []
    for f in entity.features:
        fns = ", ".join(
            f"{fn}{'(rw)' if ops.write else ''}" for fn, ops in f.operations.items())
        features.append(f"      {f.id:>2} {f.type} {f.role}" + (f": {fns}" if fns else ""))
    return f"  [{addr}] {entity.type}" + (f" - {entity.description}" if entity.description else "") + (
        "\n" + "\n".join(features) if features else "")


def _print_device(device: RemoteDevice) -> None:
    print(f"\nSPINE device {device.address} ({device.device_type}, feature set {device.feature_set})")
    for entity in device.entities:
        print(_entity_line(entity))


def _use_cases_text(device: RemoteDevice) -> str:
    lines = ["Use cases announced by the device:"]
    for info in device.use_cases():
        addr = ".".join(str(e) for e in (info.get("address") or {}).get("entity", []))
        for support in info.get("useCaseSupport", []):
            available = "" if support.get("useCaseAvailable", True) else " (not available)"
            lines.append(f"  {info.get('actor')} [{addr}] {support.get('useCaseName')} "
                         f"{support.get('useCaseVersion', '')} scenarios {support.get('scenarioSupport')}{available}")
    return "\n".join(lines)


def _value(fn, *args) -> Any:
    try:
        return fn(*args)
    except (DataNotAvailable, KeyError, IndexError, TypeError):
        return None


def _status(ucs: dict[str, UseCase], entity: RemoteEntity) -> str:
    parts = []
    if entity.type == "EVSE":
        evsecc: EVSECC = ucs["EVSECC"]
        if (m := _value(evsecc.manufacturer_data, entity)) is not None:
            parts.append(f"{m.get('brandName', '')} {m.get('deviceName', '')} "
                         f"sw {m.get('softwareRevision', '?')}".strip())
        if (s := _value(evsecc.operating_state, entity)) is not None:
            parts.append(f"state {s[0]}" + (f" error {s[1]}" if s[1] else ""))
    if entity.type != "EV":
        lpc: LPC = ucs["LPC"]
        mpc: MPC = ucs["MPC"]
        if (v := _value(lpc.consumption_limit, entity)) is not None:
            parts.append(f"LPC limit {v.value:g} W{'' if v.is_active else ' (inactive)'}"
                         + (f" for {v.duration:g} s" if v.duration else ""))
        if (v := _value(lpc.failsafe_consumption_limit, entity)) is not None:
            parts.append(f"failsafe {v:g} W")
        if (v := _value(lpc.failsafe_duration_minimum, entity)) is not None:
            parts.append(f"failsafe duration {v / 3600:g} h")
        if (v := _value(lpc.consumption_nominal_max, entity)) is not None:
            parts.append(f"nominal max {v:g} W")
        if (v := _value(mpc.power, entity)) is not None:
            parts.append(f"power {v:g} W")
        if (v := _value(mpc.current_per_phase, entity)) is not None:
            parts.append(f"current {v} A")
        if (v := _value(mpc.voltage_per_phase, entity)) is not None:
            parts.append(f"voltage {v} V")
        if (v := _value(mpc.energy_consumed, entity)) is not None:
            parts.append(f"energy {v:g} Wh")
    elif entity.type == "EV":
        evcc: EVCC = ucs["EVCC"]
        evcem: EVCEM = ucs["EVCEM"]
        opev: OPEV = ucs["OPEV"]
        oscev: OSCEV = ucs["OSCEV"]
        parts.append(f"charge state {evcc.charge_state(entity)}")
        if (v := _value(evcc.communication_standard, entity)) and v != "unknown":
            parts.append(v)
        if (v := _value(evcem.current_per_phase, entity)) is not None:
            parts.append(f"current {v} A")
        if (v := _value(evcem.power_per_phase, entity)) is not None:
            parts.append(f"power {v} W")
        if (v := _value(evcem.energy_charged, entity)) is not None:
            parts.append(f"energy {v} Wh")
        if (v := _value(opev.current_limits, entity)) is not None:
            parts.append(f"current range min {v[0]} max {v[1]} A")
        if (v := _value(opev.load_control_limits, entity)) is not None:
            parts.append("OPEV limits " + ", ".join(
                f"{lim.value:g}{'' if lim.is_active else ' (inactive)'}" for lim in v))
        if (v := _value(oscev.load_control_limits, entity)) is not None:
            parts.append("OSCEV limits " + ", ".join(
                f"{lim.value:g}{'' if lim.is_active else ' (inactive)'}" for lim in v))
    return "; ".join(parts)


async def connect(ski: str, host: str | None, port: int, local_port: int, config: Path,
                  raw: bool, read_all: bool = False, lpc_limit: float | None = None) -> int:
    identity = Identity.load_or_create(config, "pyeebus")
    trust = TrustStore.load(config / "trust.json")
    service = EebusService(identity, brand="pyeebus", model="pyeebus-cli", serial=identity.ski[:8],
                           ship_id=f"pyeebus-{identity.ski[:8]}", port=local_port, trust=trust,
                           auto_connect=host is None)
    cem = service.entities[0]
    last: dict[tuple, str] = {}

    def on_uc_event(_ski: str, entity: RemoteEntity | None, name: str) -> None:
        if entity is None or name == UseCase.USE_CASE_SUPPORT_UPDATE:
            return
        if (lpc_limit is not None and name == LPC.DATA_UPDATE_LIMIT
                and entity.device.ski not in limit_written):
            limit_written.add(entity.device.ski)
            spawn(write_limit(entity))
        line = _status(ucs, entity)
        key = (entity.device.ski, entity.entity)
        if line and line != "charge state unknown" and last.get(key) != line:
            last[key] = line
            print(f"  {entity.type} [{'.'.join(map(str, entity.entity))}]: {line}")

    ucs = {cls.__name__: cls(cem, on_uc_event).setup() for cls in (*ALL_EV_USE_CASES, LPC, MPC)}
    if read_all:
        cem.add_feature("Bill", Role.CLIENT)
    printed: set[tuple] = set()
    limit_written: set[str] = set()

    def read_everything(device: RemoteDevice) -> None:
        for entity in device.entities[1:]:
            for feature in entity.features:
                local = cem.feature(feature.type, Role.CLIENT)
                if feature.role != Role.SERVER or local is None:
                    continue
                for fn, ops in feature.operations.items():
                    if ops.read:
                        local.read(feature, fn)

    async def write_limit(entity: RemoteEntity) -> None:
        try:
            await ucs["LPC"].write_consumption_limit(entity, LoadLimit(lpc_limit, True, duration=300))
            print(f"  LPC limit {lpc_limit:g} W for 5 minutes accepted")
        except Exception as err:  # noqa: BLE001
            print(f"  LPC limit {lpc_limit:g} W failed: {err}")

    def on_spine_event(event: Event) -> None:
        if event.type == EventType.DEVICE and event.change == Change.ADD and event.device:
            _print_device(event.device)
            if read_all:
                read_everything(event.device)
        elif (read_all and event.type == EventType.DATA and event.classifier == "reply" and event.feature
              and event.function != "deviceDiagnosisHeartbeatData"
              and (key := (event.feature.address, event.function)) not in printed):
            printed.add(key)
            f = event.feature
            print(f"  [{'.'.join(map(str, f.entity.entity))}]:{f.id} {f.type} {event.function} = "
                  f"{json.dumps(event.data, separators=(',', ':'))}")
        elif event.type == EventType.DEVICE and event.change == Change.REMOVE:
            print("SPINE device gone")
        elif event.type == EventType.ENTITY and event.entity and event.change == Change.ADD:
            if event.device and event.device.address and len(event.entity.entity) > 1:
                print("\nEntity added:\n" + _entity_line(event.entity))
        elif event.type == EventType.ENTITY and event.entity and event.change == Change.REMOVE:
            print(f"\nEntity removed: {event.entity.type} {list(event.entity.entity)}")
        elif event.type == EventType.DATA and event.function == "nodeManagementUseCaseData" and event.device:
            text = _use_cases_text(event.device)
            if last.get(("use cases", event.device.ski)) != text:
                last[("use cases", event.device.ski)] = text
                print("\n" + text)

    service.device.subscribe_events(on_spine_event)
    if raw:
        service.trace = lambda _ski, direction, payload: print(
            f"{'<<' if direction == 'in' else '>>'} {json.dumps(payload, separators=(',', ':'))}")

    print(f"Our SKI: {identity.ski}")
    print("In the wallbox, pair with the EEBUS device 'pyeebus' / this SKI "
          "(Elli: Connections > HEMS connection > found EEBUS devices > Pair).\n")

    def on_state(remote: str, state: State, err: Exception | None) -> None:
        if state == State.HELLO_WAITING_FOR_TRUST:
            return
        if isinstance(err, RemoteAbortError):
            print(f"[{remote[:8]}] not paired yet: pair 'pyeebus' in the wallbox UI, retrying ...")
            return
        if state in (State.COMPLETE, State.CLOSED, State.ERROR):
            print(f"[{remote[:8]}] SHIP {state.value}" + (f": {err}" if err else ""))

    service.node.on_state = on_state
    await service.start()
    service.trust(ski)
    try:
        if host:
            while True:
                delay = 5
                try:
                    conn = await service.node.connect(host, port, ski)
                    await conn.wait_closed()
                except RemoteAbortError:
                    delay = 15
                except Exception as err:  # noqa: BLE001
                    print(f"connection failed: {err}")
                await asyncio.sleep(delay)
        else:
            print("Waiting for the device via mDNS (Ctrl+C to stop) ...")
            await asyncio.Event().wait()
    finally:
        await service.stop()


# --- simulator -------------------------------------------------------------------------------


async def simulate(config: Path, port: int, trust_ski: str | None) -> int:
    from .simulator import SimulatedEVSE

    identity = Identity.load_or_create(config / "sim-evse", "pyeebus-sim-evse")
    trust = TrustStore.load(config / "sim-evse" / "trust.json")
    sim = SimulatedEVSE(identity, port=port, trust=trust)
    if trust_ski:
        sim.service.trust(trust_ski)
    await sim.start()
    print(f"Simulated EVSE running on port {sim.service.node.port}, SKI {identity.ski}")
    print("Commands: plug, unplug, charge <A> <Wh>, limits, quit")
    loop = asyncio.get_running_loop()
    while True:
        line = (await loop.run_in_executor(None, sys.stdin.readline)).strip()
        if not line or line == "quit":
            break
        cmd, *args = line.split()
        if cmd == "plug":
            sim.plug_in()
        elif cmd == "unplug":
            sim.unplug()
        elif cmd == "charge" and len(args) == 2:
            sim.set_charging(float(args[0]), float(args[1]))
        elif cmd == "limits":
            print(sim.limits)
    await sim.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pyeebus", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("discover", help="list EEBUS devices on the network")
    sp.add_argument("--seconds", type=float, default=5)
    sp = sub.add_parser("connect", help="pair with a device and act as energy manager (CEM)")
    sp.add_argument("ski", help="SKI of the device (from 'discover' or its web UI)")
    sp.add_argument("--host", help="connect directly instead of waiting for mDNS")
    sp.add_argument("--port", type=int, default=4712)
    sp.add_argument("--local-port", type=int, default=4712)
    sp.add_argument("--config", type=Path, default=CONFIG_DIR)
    sp.add_argument("--raw", action="store_true", help="print all SPINE messages")
    sp.add_argument("--read-all", action="store_true", help="read and print all data the device offers")
    sp.add_argument("--lpc-limit", type=float, metavar="W",
                    help="test: set this power limit (LPC) for 5 minutes")
    sp = sub.add_parser("simulate-evse", help="run a simulated wallbox (for development)")
    sp.add_argument("--port", type=int, default=4713)
    sp.add_argument("--trust", help="SKI of the energy manager to accept")
    sp.add_argument("--config", type=Path, default=CONFIG_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s %(message)s")
    try:
        if args.cmd == "discover":
            return asyncio.run(discover(args.seconds))
        if args.cmd == "simulate-evse":
            return asyncio.run(simulate(args.config, args.port, args.trust))
        if not is_ski_valid(args.ski):
            parser.error("SKI must be 40 hex characters")
        return asyncio.run(connect(normalize_ski(args.ski), args.host, args.port,
                                   args.local_port, args.config, args.raw, args.read_all,
                                   args.lpc_limit))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
