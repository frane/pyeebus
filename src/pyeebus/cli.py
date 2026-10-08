"""pyeebus command line: discover SHIP devices and test a connection."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .ship import Identity, ShipNode, State, TrustStore, is_ski_valid, normalize_ski
from .ship.mdns import ShipMdns, ShipService

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


async def connect(ski: str, host: str | None, port: int, local_port: int,
                  config: Path) -> int:
    identity = Identity.load_or_create(config, "pyeebus")
    trust = TrustStore.load(config / "trust.json")
    node = ShipNode(identity, ship_id=f"pyeebus-{identity.ski[:8]}", brand="pyeebus",
                    model="pyeebus-cli", device_type="EnergyManagementSystem",
                    port=local_port, trust=trust)
    print(f"Our SKI: {identity.ski}")
    print("In the wallbox, pair with the EEBUS device 'pyeebus' / this SKI "
          "(Elli: Connections > HEMS connection > found EEBUS devices > Pair).\n")

    def on_state(remote: str, state: State, err: Exception | None) -> None:
        extra = f": {err}" if err else ""
        if state == State.HELLO_WAITING_FOR_TRUST:
            extra = " (the remote does not trust us yet, pair in the wallbox UI)"
        print(f"[{remote[:8]}] SHIP {state.value}{extra}")

    def on_connected(conn) -> None:
        print(f"[{conn.remote_ski[:8]}] connected, remote SHIP id {conn.remote_ship_id!r}. "
              "SPINE messages from the remote follow:")

        def dump(payload: dict) -> None:
            print(json.dumps(payload, indent=1)[:4000])

        conn.on_data = dump

    node.on_state = on_state
    node.on_connected = on_connected
    await node.start()
    node.trust_ski(ski)
    try:
        if host:
            while True:
                try:
                    conn = await node.connect(host, port, ski)
                    await conn.wait_closed()
                except Exception as err:  # noqa: BLE001
                    print(f"connection failed: {err}")
                await asyncio.sleep(5)
        else:
            print("Waiting for the device via mDNS (Ctrl+C to stop) ...")
            await asyncio.Event().wait()
    finally:
        await node.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pyeebus", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("discover", help="list EEBUS devices on the network")
    sp.add_argument("--seconds", type=float, default=5)
    sp = sub.add_parser("connect", help="pair with and connect to a device by SKI")
    sp.add_argument("ski", help="SKI of the device (from 'discover' or its web UI)")
    sp.add_argument("--host", help="connect directly instead of waiting for mDNS")
    sp.add_argument("--port", type=int, default=4712)
    sp.add_argument("--local-port", type=int, default=4712)
    sp.add_argument("--config", type=Path, default=CONFIG_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s %(message)s")
    try:
        if args.cmd == "discover":
            return asyncio.run(discover(args.seconds))
        if not is_ski_valid(args.ski):
            parser.error("SKI must be 40 hex characters")
        return asyncio.run(connect(normalize_ski(args.ski), args.host, args.port,
                                   args.local_port, args.config))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
