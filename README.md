# pyeebus

EEBUS for Python: SHIP (transport), SPINE (data model) and the energy manager (CEM) use cases for EV charging. Ported from the Go implementation by [enbility](https://enbility.net/) (ship-go, spine-go, eebus-go, MIT licensed).

> **Status: early.** SHIP, SPINE and the EV charging use cases work and are tested against eebus-go/spine-go in both directions. Pairing with a real Elli Charger 2 works on the SHIP level; the SPINE exchange with real wallboxes is being tested.

## Why

EEBUS is how energy managers talk to wallboxes, heat pumps and grid control boxes. Until now, the only complete open-source implementation has been in Go. pyeebus makes EEBUS available to Python projects such as Home Assistant integrations.

## Roadmap

| Layer | Status |
|---|---|
| SHIP: certificates/SKI, TLS websocket, handshake (CMI, hello incl. prolongation, protocol, PIN none, access methods), data exchange, close | ✅ tested against ship-go |
| SHIP: mDNS announce and discovery (`zeroconf`), trust store, auto-reconnect | ✅ |
| SPINE: devices/entities/features, detailed discovery, use case data, subscriptions, bindings, partial updates, heartbeat | ✅ tested against spine-go |
| Use cases (CEM side): EVSECC, EVCC, EVCEM, OPEV, OSCEV, EVSOC | ✅ tested against eebus-go |
| Coordinated EV charging (CEVC), grid use cases (LPC etc.) | later |

## Try it

```bash
pip install pyeebus   # or: uvx pyeebus ...

pyeebus discover                     # list EEBUS devices on the network
pyeebus connect <SKI of the device>  # pair, connect as energy manager, show what the device offers
pyeebus connect <SKI> --raw          # ... and print every SPINE message
```

`connect` announces pyeebus on the network as an energy manager. Then pair with it on the device. For example, on an Elli wallbox: *Connections → HEMS connection → found EEBUS devices → Pair*. The identity is kept in `~/.config/pyeebus/`.

`pyeebus simulate-evse` runs a simulated wallbox with a pluggable EV for development.

## Library

```python
from pyeebus.service import EebusService
from pyeebus.usecases import EVCC, EVCEM, EVSECC, OPEV, PhaseLimit

service = EebusService.from_directory("/path/to/state", brand="Me", model="HEMS", serial="1")
cem = service.entities[0]                      # the CEM entity

def on_event(ski, entity, event):              # e.g. "EvConnected", "DataUpdateCurrentPerPhase"
    if event == EVCEM.DATA_UPDATE_CURRENT_PER_PHASE:
        print(evcem.current_per_phase(entity))

evsecc, evcc, evcem, opev = (uc(cem, on_event).setup() for uc in (EVSECC, EVCC, EVCEM, OPEV))

await service.start()
service.trust("<SKI of the wallbox>")          # connects as soon as it is found via mDNS

# later, with the EV entity from an event:
await opev.write_load_control_limits(ev, [PhaseLimit(p, 10) for p in "abc"])   # 10 A
```

The use cases follow eebus-go: `evcc.charge_state(ev)`, `evcc.communication_standard(ev)`, `evcem.energy_charged(ev)`, `opev.current_limits(ev)` (min/max/default per phase) and so on. Values the device has not sent yet raise `DataNotAvailable`.

Lower levels are usable on their own: `pyeebus.ship` (`ShipNode`, `ShipConnection`) and `pyeebus.spine` (`LocalDevice`, features, subscriptions).

**Limitation of Python's `ssl`:** a server cannot request a client certificate without verifying it. For that reason pyeebus accepts incoming connections only from peers whose certificate it already knows, for example from an earlier outgoing connection. It reaches new peers as a client.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
pytest
```

The interop tests against ship-go and eebus-go run when `SHIP_GO_HARNESS` and `EEBUS_GO_HARNESS` point to the built harnesses; see [interop/](interop). CI builds them automatically. `tools/gen_schema.py` regenerates the SPINE list/key metadata from spine-go's model.

*EEBUS is a trademark of EEBus Initiative e.V. This project is not affiliated with it.*
