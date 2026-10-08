"""SPINE tests: data updates, and a CEM talking to the simulated EVSE over SHIP."""

from __future__ import annotations

import asyncio

import pytest

from pyeebus.service import EebusService
from pyeebus.ship import Identity
from pyeebus.simulator import SimulatedEVSE
from pyeebus.spine import scaled_number, scaled_value
from pyeebus.spine.model import format_duration, parse_duration
from pyeebus.spine.update import update_data
from pyeebus.usecases import EVCC, EVCEM, EVSECC, OPEV, OSCEV, DataNotAvailable, PhaseLimit

PARTIAL = [{"cmdControl": {"partial": {}}}]

# --- values and updates --------------------------------------------------------------------


def test_scaled_numbers_and_durations():
    assert scaled_number(16) == {"number": 16, "scale": 0}
    assert scaled_number(6.5) == {"number": 65, "scale": -1}
    assert scaled_value({"number": 4140, "scale": 0}) == 4140
    assert scaled_value({"number": 65, "scale": -1}) == pytest.approx(6.5)
    assert parse_duration("PT4S") == 4
    assert parse_duration("P1DT2H") == 93600
    assert format_duration(4) == "PT4S"
    assert format_duration(3600) == "PT1H"


def test_full_update_replaces():
    old = {"measurementData": [{"measurementId": 1, "value": {"number": 1}}]}
    new = {"measurementData": [{"measurementId": 2, "value": {"number": 2}}]}
    assert update_data("measurementListData", old, new) == new


def test_partial_update_merges_by_key():
    old = {"measurementData": [
        {"measurementId": 1, "valueType": "value", "value": {"number": 1}},
        {"measurementId": 2, "valueType": "value", "value": {"number": 2}}]}
    new = {"measurementData": [{"measurementId": 2, "valueType": "value", "value": {"number": 5}},
                               {"measurementId": 3, "valueType": "value", "value": {"number": 3}}]}
    result = update_data("measurementListData", old, new, PARTIAL)
    assert [m["value"]["number"] for m in result["measurementData"]] == [1, 5, 3]


def test_partial_update_ignores_key_only_items_and_keeps_fields():
    old = {"loadControlLimitData": [{"limitId": 1, "isLimitActive": False, "value": {"number": 16}}]}
    result = update_data("loadControlLimitListData", old, {"loadControlLimitData": [{"limitId": 1}]}, PARTIAL)
    assert result == old
    result = update_data("loadControlLimitListData", old,
                         {"loadControlLimitData": [{"limitId": 1, "isLimitActive": True}]}, PARTIAL)
    assert result["loadControlLimitData"][0] == {"limitId": 1, "isLimitActive": True, "value": {"number": 16}}


def test_delete_filter():
    old = {"loadControlLimitData": [{"limitId": 1, "value": {"number": 1}, "timePeriod": {"endTime": "PT1H"}},
                                    {"limitId": 2, "value": {"number": 2}}]}
    delete = [{"cmdControl": {"delete": {}}, "loadControlLimitListDataSelectors": {"limitId": 2}}]
    assert update_data("loadControlLimitListData", old, {}, delete)["loadControlLimitData"] == old[
        "loadControlLimitData"][:1]
    delete = [{"cmdControl": {"delete": {}}, "loadControlLimitListDataSelectors": {"limitId": 1},
               "loadControlLimitDataElements": {"timePeriod": {}}}]
    result = update_data("loadControlLimitListData", old, {}, delete)
    assert "timePeriod" not in result["loadControlLimitData"][0]


# --- CEM <-> simulated EVSE over SHIP ---------------------------------------------------------


async def wait_for(condition, timeout: float = 5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        try:
            result = condition()
            if result:
                return result
        except DataNotAvailable:
            pass
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


@pytest.fixture
async def cem_and_evse():
    node = {"announce": False, "discover": False, "host": "127.0.0.1"}
    evse = SimulatedEVSE(Identity.create("evse"), port=0, **node)
    cem = EebusService(Identity.create("cem"), brand="pyeebus", model="TestCEM", serial="1", port=0, **node)
    events: list[tuple[str, str]] = []

    def on_event(_ski, entity, name):
        events.append((entity.type if entity else "", name))

    ucs = {cls.__name__: cls(cem.entities[0], on_event).setup()
           for cls in (EVSECC, EVCC, EVCEM, OPEV, OSCEV)}
    evse.service.node.trust.trust(cem.ski, cem.identity.cert_pem)
    cem.node.trust.trust(evse.service.ski)
    await evse.start()
    await cem.start()
    await cem.node.connect("127.0.0.1", evse.service.node.port, evse.service.ski)
    yield cem, evse, ucs, events
    await cem.stop()
    await evse.stop()


async def test_cem_discovers_evse_and_ev(cem_and_evse):
    cem, evse, ucs, events = cem_and_evse
    remote = await wait_for(lambda: next(iter(cem.remote_devices.values()), None))
    evse_entity = await wait_for(lambda: next((e for e in remote.entities if e.type == "EVSE"), None))
    assert remote.address == evse.device.address
    assert (await wait_for(lambda: ucs["EVSECC"].manufacturer_data(evse_entity)))["deviceName"] == "SimEVSE"
    assert ucs["EVSECC"].operating_state(evse_entity) == ("normalOperation", "")
    # the EVSE subscribed to nothing, but we subscribed to its node management
    assert await wait_for(lambda: any(s[0].device == cem.device.address for s in evse.device.subscriptions))

    evse.plug_in()
    ev = await wait_for(lambda: remote.entity((1, 1)))
    assert ev.type == "EV"
    assert await wait_for(lambda: ucs["OPEV"].is_scenario_available(ev, 1))
    limits = await wait_for(lambda: ucs["OPEV"].load_control_limits(ev))
    assert [lim.value for lim in limits] == [16, 16, 16]
    assert ucs["OPEV"].current_limits(ev) == ([6, 6, 6], [16, 16, 16], [0, 0, 0])
    assert await wait_for(lambda: ucs["EVCC"].communication_standard(ev) == "iec61851")
    assert ucs["EVCC"].charging_power_limits(ev) == (4140, 11040, 0)
    assert await wait_for(lambda: ucs["EVCC"].identifications(ev)) == [("eui48", "02:00:00:00:00:01")]
    assert ucs["EVCC"].charge_state(ev) == "active"
    assert ("EV", EVCC.EV_CONNECTED) in events

    # write limits (binds first if OPEV's binding is not done yet)
    await ucs["OPEV"].write_load_control_limits(ev, [PhaseLimit(p, 10) for p in "abc"])
    assert evse.limits["obligation"] == [10, 10, 10]
    await wait_for(lambda: [lim.value for lim in ucs["OPEV"].load_control_limits(ev)] == [10, 10, 10])
    # below the minimum -> default (0 = pause), above max -> max
    await ucs["OPEV"].write_load_control_limits(ev, [PhaseLimit("a", 3), PhaseLimit("b", 40),
                                                     PhaseLimit("c", 8)])
    assert evse.limits["obligation"] == [0, 16, 8]

    # measurements arrive by notify
    evse.set_charging(10, 1234)
    assert await wait_for(lambda: ucs["EVCEM"].current_per_phase(ev) == [10, 10, 10])
    assert ucs["EVCEM"].power_per_phase(ev) == [2300, 2300, 2300]
    assert ucs["EVCEM"].energy_charged(ev) == 1234
    assert ucs["EVCEM"].phases_connected(ev) == 3

    evse.unplug()
    await wait_for(lambda: remote.entity((1, 1)) is None)
    assert ("EV", EVCC.EV_DISCONNECTED) in events
    assert ucs["EVCC"].charge_state(None) == "unplugged"


async def test_heartbeat_is_notified_to_subscribers(cem_and_evse):
    cem, evse, _ucs, _events = cem_and_evse
    remote_cem = await wait_for(lambda: next(iter(evse.service.remote_devices.values()), None))
    cem_entity = await wait_for(lambda: remote_cem.entity((1,)))
    diag = await wait_for(lambda: cem_entity.feature("DeviceDiagnosis", "server"))
    client = evse.evse.add_feature("DeviceDiagnosis", "client")
    evse.device.announce_entity(evse.evse)  # tell the CEM about the new feature
    remote_evse = await wait_for(lambda: next(iter(cem.remote_devices.values()), None))
    await wait_for(lambda: remote_evse.entity((1,)).feature("DeviceDiagnosis", "client"))
    await client.subscribe(diag)
    first = (await client.request(diag, "deviceDiagnosisHeartbeatData"))["heartbeatCounter"]
    assert await wait_for(lambda: diag.data["deviceDiagnosisHeartbeatData"]["heartbeatCounter"] > first,
                          timeout=4)
