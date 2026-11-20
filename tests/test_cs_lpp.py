"""CS-LPP next to CS-LPC on one controllable system, controlled by the pyeebus Energy Guard (LPC + LPP)."""

from __future__ import annotations

import asyncio

import pytest

from pyeebus.service import EebusService
from pyeebus.ship import Identity
from pyeebus.spine.model import SpineError, scaled_number, scaled_value
from pyeebus.usecases import (
    LPC,
    LPP,
    CsLPC,
    CsLPP,
    DataNotAvailable,
    LimitationState,
    LoadLimit,
    MonitorPowerConfig,
    MuMPC,
)

NODE = {"announce": False, "discover": False, "host": "127.0.0.1"}


async def wait_for(condition, timeout: float = 5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        try:
            if result := condition():
                return result
        except DataNotAvailable:
            pass
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_local_lpp_next_to_lpc():
    cs = EebusService(Identity.create("cs"), brand="pyeebus", model="TestCS", serial="2", port=0,
                      device_type="Inverter", entity_types=("Inverter",), **NODE)
    entity = cs.entities[0]
    mpc = MuMPC(entity, power=MonitorPowerConfig("abc")).setup()
    with pytest.raises(TypeError):
        CsLPP(entity)  # the failsafe limit is required
    with pytest.raises(ValueError):
        CsLPP(entity, failsafe_limit=6000, failsafe_duration=3600)
    lpc = CsLPC(entity, failsafe_limit=2000).setup()
    lpp = CsLPP(entity, failsafe_limit=6000, failsafe_duration=4 * 3600).setup()
    try:
        lc = entity.feature("LoadControl", "server")
        descs = lc.data["loadControlLimitDescriptionListData"]["loadControlLimitDescriptionData"]
        assert [(d["limitId"], d["limitDirection"], d["measurementId"]) for d in descs] == [
            (0, "consume", mpc.ids["powerTotal"]), (1, "produce", mpc.ids["powerTotal"])]
        dc = entity.feature("DeviceConfiguration", "server")
        keys = [d["keyName"] for d in dc.data["deviceConfigurationKeyValueDescriptionListData"][
            "deviceConfigurationKeyValueDescriptionData"]]
        assert keys == ["failsafeConsumptionActivePowerLimit", "failsafeDurationMinimum",
                        "failsafeProductionActivePowerLimit"]  # the duration is shared
        ec = entity.feature("ElectricalConnection", "server")
        chars = ec.data["electricalConnectionCharacteristicListData"]["electricalConnectionCharacteristicData"]
        assert [c["characteristicType"] for c in chars] == ["powerConsumptionNominalMax", "powerProductionNominalMax"]

        lpp.set_production_nominal_max(10000)
        lpc.set_consumption_nominal_max(3000)
        assert (lpp.production_nominal_max(), lpc.consumption_nominal_max()) == (10000, 3000)
        assert lpp.failsafe_production_limit() == (6000, True)
        assert lpc.failsafe_limit() == (2000, True)
        # the duration is shared: CS-LPC was set up first, so its start value (2 h) applies
        assert lpp.failsafe_duration_minimum() == (2 * 3600, True)
        lpp.set_failsafe_duration_minimum(4 * 3600)
        assert lpc.failsafe_duration_minimum() == (4 * 3600, True)
        lpp.set_production_limit(LoadLimit(7000, is_active=False))
        assert lpp.production_limit() == LoadLimit(7000, is_active=False)
        # LPP's passive sign convention: the limit is <= 0 on the wire [LPP-001]
        items = lc.data["loadControlLimitListData"]["loadControlLimitData"]
        assert {i["limitId"]: scaled_value(i["value"]) for i in items} == {0: 0, 1: -7000}
        assert lpc.consumption_limit().value == 0
        assert (lpp.state, lpp.effective_limit()) == (LimitationState.INIT, 6000)
        use_cases = cs.device.node_management.data["nodeManagementUseCaseData"]["useCaseInformation"]
        names = [u["useCaseName"] for i in use_cases for u in i["useCaseSupport"]]
        assert names == ["monitoringOfPowerConsumption", "limitationOfPowerConsumption",
                         "limitationOfPowerProduction"]
    finally:
        lpc.close()
        lpp.close()


@pytest.fixture
async def guard_and_cs():
    eg = EebusService(Identity.create("eg"), brand="pyeebus", model="TestEG", serial="1", port=0, **NODE)
    cs = EebusService(Identity.create("cs"), brand="pyeebus", model="TestCS", serial="2", port=0, **NODE)
    events: list[tuple[str, str]] = []
    clock = Clock()
    eg_lpc, eg_lpp = LPC(eg.entities[0]).setup(), LPP(eg.entities[0]).setup()

    def deny_high(limit: LoadLimit) -> str | None:
        return "above the inverter" if limit.is_active and limit.value > 10000 else None

    lpc = CsLPC(cs.entities[0], lambda _s, _e, name: events.append(("lpc", name)),
                failsafe_limit=4200, clock=clock).setup()
    lpp = CsLPP(cs.entities[0], lambda _s, _e, name: events.append(("lpp", name)),
                failsafe_limit=6000, approve_limit=deny_high, clock=clock).setup()
    lpp.set_production_nominal_max(10000)
    lpp.set_production_limit(LoadLimit(10000, is_active=False))

    cs.node.trust.trust(eg.ski, eg.identity.cert_pem)
    eg.node.trust.trust(cs.ski)
    await cs.start()
    await eg.start()
    await eg.node.connect("127.0.0.1", cs.node.port, cs.ski)
    remote = await wait_for(lambda: next(iter(eg.remote_devices.values()), None))
    entity = await wait_for(lambda: next((e for e in remote.entities if e.type == "CEM"), None))
    yield eg_lpc, eg_lpp, lpc, lpp, entity, events, clock
    lpc.close()
    lpp.close()
    await eg.stop()
    await cs.stop()


async def test_energy_guard_controls_production(guard_and_cs):
    eg_lpc, eg_lpp, lpc, lpp, entity, events, clock = guard_and_cs

    assert await wait_for(lambda: eg_lpp.is_scenario_available(entity, 1))
    assert await wait_for(lambda: eg_lpp.production_nominal_max(entity)) == 10000  # contractual (HEMS)
    limit = await wait_for(lambda: eg_lpp.production_limit(entity))
    assert (limit.value, limit.is_active) == (10000, False)
    assert await wait_for(lambda: eg_lpp.failsafe_production_limit(entity)) == 6000
    assert await wait_for(lambda: eg_lpc.failsafe_consumption_limit(entity)) == 4200
    assert eg_lpp.failsafe_duration_minimum(entity) == 2 * 3600
    await wait_for(lpp.is_heartbeat_within_duration)

    # a denied write after a heartbeat leaves init as well, without a limit
    with pytest.raises(SpineError):
        await eg_lpp.write_production_limit(entity, LoadLimit(12000, True))
    assert (lpp.state, lpp.effective_limit()) == (LimitationState.UNLIMITED_CONTROLLED, None)
    assert lpc.state == LimitationState.INIT

    # a feed-in limit only changes LPP; it goes negative on the wire and the guard reads it back
    await eg_lpp.write_production_limit(entity, LoadLimit(4000, True))
    assert lpp._limit_item()["value"] == scaled_number(-4000)
    assert (await wait_for(lambda: eg_lpp.production_limit(entity).is_active and
                           eg_lpp.production_limit(entity))).value == 4000
    assert (lpp.state, lpp.effective_limit()) == (LimitationState.LIMITED, 4000)
    assert lpc.state == LimitationState.INIT
    assert lpc.consumption_limit().value == 0
    assert ("lpp", CsLPP.DATA_UPDATE_LIMIT) in events
    assert ("lpc", CsLPC.DATA_UPDATE_LIMIT) not in events
    await eg_lpc.write_consumption_limit(entity, LoadLimit(4200, False))
    assert lpc.state == LimitationState.UNLIMITED_CONTROLLED
    assert lpp.production_limit().value == 4000

    with pytest.raises(SpineError):
        await eg_lpp.write_production_limit(entity, LoadLimit(12000, True))
    assert lpp.production_limit().value == 4000

    # failsafe values: the production limit is LPP's, the duration is shared
    await eg_lpp.write_failsafe_production_limit(entity, 5000)
    assert lpp.failsafe_production_limit() == (5000, True)
    assert lpc.failsafe_consumption_limit() == (4200, True)
    assert ("lpp", CsLPP.DATA_UPDATE_FAILSAFE_POWER) in events
    assert ("lpc", CsLPC.DATA_UPDATE_FAILSAFE_POWER) not in events
    await eg_lpp.write_failsafe_duration_minimum(entity, 3 * 3600)
    assert lpc.failsafe_duration_minimum() == (3 * 3600, True)
    assert ("lpc", CsLPC.DATA_UPDATE_FAILSAFE_DURATION) in events

    # heartbeat lost: both go to failsafe with their own limits
    clock.now += 121
    lpc.tick()
    lpp.tick()
    assert (lpp.state, lpp.effective_limit()) == (LimitationState.FAILSAFE, 5000)
    assert (lpc.state, lpc.effective_limit()) == (LimitationState.FAILSAFE, 4200)
    await wait_for(lpp.is_heartbeat_within_duration)
    await eg_lpp.write_production_limit(entity, LoadLimit(3000, True))
    assert (lpp.state, lpp.effective_limit()) == (LimitationState.LIMITED, 3000)
    assert lpc.state == LimitationState.FAILSAFE

    # LPC gets no limit while the heartbeat is back: unlimited/autonomous after 120 s [LPC-921]
    clock.now += 60
    await wait_for(lambda: lpc.machine.last_heartbeat == clock.now)
    clock.now += 61
    lpc.tick()
    assert (lpc.state, lpc.effective_limit()) == (LimitationState.UNLIMITED_AUTONOMOUS, None)
