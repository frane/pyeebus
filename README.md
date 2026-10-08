# pyeebus

EEBUS for Python: SHIP (transport) and, step by step, SPINE (data model) and the use cases for EV charging. Ported from the Go implementation by [enbility](https://enbility.net/) (ship-go, spine-go, eebus-go, MIT licensed).

> **Status: early.** The SHIP layer works and has been tested against ship-go in both directions. SPINE and the use cases are next.

## Why

EEBUS is how energy managers talk to wallboxes, heat pumps and grid control boxes. Until now, the only complete open-source implementation has been in Go. pyeebus makes EEBUS available to Python projects such as Home Assistant integrations.

## Roadmap

| Layer | Status |
|---|---|
| SHIP: certificates/SKI, TLS websocket, handshake (CMI, hello incl. prolongation, protocol, PIN none, access methods), data exchange, close | ✅ tested against ship-go |
| SHIP: mDNS announce and discovery (`zeroconf`), trust store, auto-reconnect | ✅ |
| SPINE: device model, node management, detailed discovery, subscriptions, bindings | next |
| Use cases (CEM side): EVSE/EV commissioning, EV measurements, overload protection (OPEV), self-consumption (OSCEV), EV state of charge | planned |

## Try it

```bash
pip install pyeebus   # or: uvx pyeebus ...

pyeebus discover                     # list EEBUS devices on the network
pyeebus connect <SKI of the device>  # pair and connect, print SPINE traffic
```

`connect` announces pyeebus on the network as an energy manager. Then pair with it on the device. For example, on an Elli wallbox: *Connections → HEMS connection → found EEBUS devices → Pair*. The identity is kept in `~/.config/pyeebus/`.

## Library

```python
from pyeebus.ship import Identity, ShipNode, TrustStore

identity = Identity.load_or_create("/path/to/state", "my-hems")
node = ShipNode(identity, ship_id="my-hems", brand="Me", model="HEMS",
                device_type="EnergyManagementSystem",
                trust=TrustStore.load("/path/to/state/trust.json"))

async def on_connected(conn):
    conn.on_data = lambda payload: print(payload)    # SPINE datagrams
    await conn.send_spine({"datagram": {...}})

node.on_connected = on_connected
await node.start()
node.trust_ski("<remote SKI>")   # connects as soon as it is found via mDNS
```

**Limitation of Python's `ssl`:** a server cannot request a client certificate without verifying it. For that reason pyeebus accepts incoming connections only from peers whose certificate it already knows, for example from an earlier outgoing connection. It reaches new peers as a client.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
pytest
```

The interop tests against ship-go run when `SHIP_GO_HARNESS` points to the built harness; see [interop/ship-go-harness](interop/ship-go-harness). CI builds it automatically.

*EEBUS is a trademark of EEBus Initiative e.V. This project is not affiliated with it.*
