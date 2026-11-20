# Changelog

## 0.4.0 (2026-11-20)

### Added
- **LPP**, limitation of power production (e.g. a feed-in limit), on both sides: `CsLPP` for
  the Controllable System (eebus-go cs/lpp) and `LPP` for the Energy Guard (eebus-go eg/lpp). The
  API uses positive watts of production; on the wire the limit follows LPP's passive sign
  convention (<= 0 W, a positive one is rejected) [LPP-001]. CS-LPP runs its own LPP 1.0.0
  state machine next to CS-LPC and shares the failsafe duration with it. Tested against
  eebus-go's Energy Guard. Contributed by [@dneprojects](https://github.com/dneprojects)
  ([#4](https://github.com/frane/pyeebus/pull/4)).

### Changed
- The CS-LPC logic moved into `CsPowerLimitation`, shared by `CsLPC` and `CsLPP`; the state
  machine is now `LimitationState` / `LimitationStateMachine`. The 0.2.0 names (`CsLPC`
  methods, `LPCState`, `LPCStateMachine`) keep working (@dneprojects, #4).

## 0.3.0 (2026-11-19)

### Added
- **MU-MPC**, the Monitored Unit side of monitoring of power consumption, in
  `pyeebus.usecases.mu`: a device publishes its power (total and per phase), energy consumed
  and produced, currents, voltages (per phase and phase to phase) and frequency for an Energy
  Guard or another Monitoring Appliance, with `MuMPC.update(...)`. Set up before `CsLPC`, the
  LPC limit refers to its power measurement. Tested against eebus-go's Monitoring Appliance.
  Contributed by [@dneprojects](https://github.com/dneprojects)
  ([#3](https://github.com/frane/pyeebus/pull/3)).

### Fixed
- MA-MPC: a phase-to-phase voltage (e.g. a to b) no longer replaces the voltage of phase a
  (@dneprojects, #3).

## 0.2.0 (2026-11-18)

### Added
- **CS-LPC**, the Controllable System side of limitation of power consumption, in
  `pyeebus.usecases.cs`: active power limit, failsafe limit and duration, heartbeat and
  nominal maximum, with approval callbacks for writes of the Energy Guard. `CsLPC` also runs
  the LPC 1.0.0 state machine (init, unlimited/controlled, limited, failsafe,
  unlimited/autonomous), so the application only applies `effective_limit()`. Tested against
  eebus-go's Energy Guard. Contributed by [@dneprojects](https://github.com/dneprojects)
  ([#2](https://github.com/frane/pyeebus/pull/2)).
- eebus-go interop harness: Energy Guard mode writes failsafe values and stops/starts its
  heartbeat; Docker build instructions (@dneprojects).

## 0.1.2 (2026-11-17)

First release on PyPI (`pip install pyeebus`).

### Fixed
- OPEV: an empty permitted value set is unknown, not a maximum of 0 A. First-generation Elli
  wallboxes send these right after plug-in.

## 0.1.1 (2026-11-03)

### Fixed
- Wallbox certificates that are not strict DER (BasicConstraints `cA` as `0x01`, seen on
  first-generation Elli wallboxes) are accepted; the SKI is still checked against the key.
- No blocking file access in the event loop: TLS contexts are built once in a thread, the
  trust store is written in the executor and only when it changes.

## 0.1.0 (2026-10-09)

First release: SHIP (certificates, TLS websocket, handshake, mDNS), SPINE (devices,
entities, features, discovery, subscriptions, bindings, partial updates, heartbeat), the CEM
use cases for EV charging (EVSECC, EVCC, EVCEM, OPEV, OSCEV, EVSOC) and LPC/MPC as Energy Guard
and Monitoring Appliance, tested against ship-go, spine-go and eebus-go.
