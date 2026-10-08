"""Power limitation and monitoring use cases (port of eebus-go's eg/lpc and ma/mpc).

* :class:`LPC` limitation of power consumption, Energy Guard side: set the
  active power limit of a controllable system (e.g. a wallbox under §14a EnWG),
  its failsafe limit and failsafe duration.
* :class:`MPC` monitoring of power consumption, Monitoring Appliance side:
  power, energy, currents, voltages and frequency of a monitored unit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ..spine.device import Event, RemoteEntity, delete_filter, partial_filter
from ..spine.model import (
    Role,
    as_list,
    filter_items,
    format_duration,
    parse_datetime,
    parse_duration,
    scaled_number,
    scaled_value,
    utcnow,
)
from .base import (
    DEVICE_CONFIGURATION,
    DEVICE_DIAGNOSIS,
    ELECTRICAL_CONNECTION,
    LOAD_CONTROL,
    MEASUREMENT,
    PHASES,
    ClientFeature,
    DataNotAvailable,
    Scenario,
    UseCase,
)

FN_LIMIT_DESCRIPTIONS = "loadControlLimitDescriptionListData"
FN_LIMITS = "loadControlLimitListData"
FN_CONFIG_DESCRIPTIONS = "deviceConfigurationKeyValueDescriptionListData"
FN_CONFIG_VALUES = "deviceConfigurationKeyValueListData"
FN_HEARTBEAT = "deviceDiagnosisHeartbeatData"
FN_EC_DESCRIPTIONS = "electricalConnectionDescriptionListData"
FN_EC_PARAMETERS = "electricalConnectionParameterDescriptionListData"
FN_EC_CHARACTERISTICS = "electricalConnectionCharacteristicListData"
FN_MEAS_DESCRIPTIONS = "measurementDescriptionListData"
FN_MEAS_CONSTRAINTS = "measurementConstraintsListData"
FN_MEASUREMENTS = "measurementListData"

KEY_FAILSAFE_POWER = "failsafeConsumptionActivePowerLimit"
KEY_FAILSAFE_DURATION = "failsafeDurationMinimum"


class DataInvalid(DataNotAvailable):
    """The device reports the value with a state other than normal."""


@dataclass
class LoadLimit:
    """An active power limit in W; ``duration`` in seconds (None: no end)."""

    value: float
    is_active: bool = True
    is_changeable: bool = True
    duration: float | None = None
    delete_duration: bool = False


# --- LPC -----------------------------------------------------------------------------------


class LPC(UseCase):
    actor = "EnergyGuard"
    name = "limitationOfPowerConsumption"
    version = "1.0.0"
    scenarios = (Scenario(1, True, (LOAD_CONTROL,)), Scenario(2, True, (DEVICE_CONFIGURATION,)),
                 Scenario(3, True, (DEVICE_DIAGNOSIS,)), Scenario(4, False, (ELECTRICAL_CONNECTION,)))
    valid_actors = ("ControllableSystem",)
    valid_entity_types = ("CEM", "Compressor", "EVSE", "HeatPumpAppliance", "Inverter",
                          "SmartEnergyAppliance", "SubMeterElectricity")
    client_features = (DEVICE_DIAGNOSIS, LOAD_CONTROL, DEVICE_CONFIGURATION, ELECTRICAL_CONNECTION)

    DATA_UPDATE_LIMIT = "DataUpdateLimit"
    DATA_UPDATE_FAILSAFE_POWER = "DataUpdateFailsafeConsumptionActivePowerLimit"
    DATA_UPDATE_FAILSAFE_DURATION = "DataUpdateFailsafeDurationMinimum"
    DATA_UPDATE_HEARTBEAT = "DataUpdateHeartbeat"
    DATA_UPDATE_NOMINAL_MAX = "DataUpdatePowerConsumptionNominalMax"

    LIMIT_FILTER: ClassVar[dict[str, str]] = {"limitType": "signDependentAbsValueLimit", "limitDirection": "consume",
                    "scopeType": "activePowerLimit"}

    def add_features(self) -> None:
        super().add_features()
        feature = self.local.add_feature(DEVICE_DIAGNOSIS, Role.SERVER)
        feature.add_function(FN_HEARTBEAT, read=True)

    @staticmethod
    def _characteristic_type(entity: RemoteEntity | None) -> str:
        if entity is not None and entity.type == "CEM":
            return "contractualConsumptionNominalMax"
        return "powerConsumptionNominalMax"

    def handle_entity_event(self, event: Event) -> None:
        entity = event.entity
        if self.is_entity_added(event):
            if f := ClientFeature.find(LOAD_CONTROL, self.local, entity):
                f.subscribe()
                f.bind()
                f.request(FN_LIMIT_DESCRIPTIONS)
            if f := ClientFeature.find(DEVICE_CONFIGURATION, self.local, entity):
                f.subscribe()
                f.bind()
                f.request(FN_CONFIG_DESCRIPTIONS)
            if f := ClientFeature.find(DEVICE_DIAGNOSIS, self.local, entity):
                f.subscribe()
                f.request(FN_HEARTBEAT)
            if f := ClientFeature.find(ELECTRICAL_CONNECTION, self.local, entity):
                f.subscribe()
                f.request(FN_EC_CHARACTERISTICS)
        elif self.is_data_update(event, FN_HEARTBEAT) and event.classifier == "notify":
            self._emit(event.ski, entity, self.DATA_UPDATE_HEARTBEAT)
        elif self.is_data_update(event, FN_LIMIT_DESCRIPTIONS):
            if f := ClientFeature.find(LOAD_CONTROL, self.local, entity):
                f.request(FN_LIMITS)
        elif self.is_data_update(event, FN_LIMITS):
            try:
                desc = self._limit_description(entity)
            except DataNotAvailable:
                return
            if any(i.get("limitId") == desc.get("limitId")
                   for i in as_list((event.data or {}).get("loadControlLimitData"))):
                self._emit(event.ski, entity, self.DATA_UPDATE_LIMIT)
        elif self.is_data_update(event, FN_CONFIG_DESCRIPTIONS):
            if f := ClientFeature.find(DEVICE_CONFIGURATION, self.local, entity):
                f.request(FN_CONFIG_VALUES)
        elif self.is_data_update(event, FN_CONFIG_VALUES):
            self._emit(event.ski, entity, self.DATA_UPDATE_FAILSAFE_POWER)
            self._emit(event.ski, entity, self.DATA_UPDATE_FAILSAFE_DURATION)
        elif self.is_data_update(event, FN_EC_CHARACTERISTICS):
            self._emit(event.ski, entity, self.DATA_UPDATE_NOMINAL_MAX)

    # limit
    def _limit_description(self, entity: RemoteEntity) -> dict[str, Any]:
        f = ClientFeature.find(LOAD_CONTROL, self.local, entity)
        if f is None:
            raise DataNotAvailable("no load control feature")
        descs = filter_items(f.items(FN_LIMIT_DESCRIPTIONS, "loadControlLimitDescriptionData"),
                             self.LIMIT_FILTER)
        if len(descs) != 1 or descs[0].get("limitId") is None:
            raise DataNotAvailable("no active power limit description")
        return descs[0]

    def consumption_limit(self, entity: RemoteEntity) -> LoadLimit:
        """The current active power consumption limit (W)."""
        desc = self._limit_description(entity)
        f = ClientFeature(LOAD_CONTROL, self.local, entity)
        item = next((i for i in f.items(FN_LIMITS, "loadControlLimitData")
                     if i.get("limitId") == desc["limitId"]), None)
        if item is None or scaled_value(item.get("value")) is None:
            raise DataNotAvailable("no limit value")
        duration = None
        end = (item.get("timePeriod") or {}).get("endTime")
        if end:
            try:
                duration = parse_duration(end)
            except ValueError:
                try:
                    duration = (parse_datetime(end) - utcnow()).total_seconds()
                except ValueError:
                    duration = None
        return LoadLimit(scaled_value(item["value"]), bool(item.get("isLimitActive")),
                         bool(item.get("isLimitChangeable")), duration)

    async def write_consumption_limit(self, entity: RemoteEntity, limit: LoadLimit,
                                      timeout: float | None = None) -> None:
        """Set the active power consumption limit; raises SpineError if rejected."""
        desc = self._limit_description(entity)
        f = ClientFeature(LOAD_CONTROL, self.local, entity)
        current = [i for i in f.items(FN_LIMITS, "loadControlLimitData") if i.get("limitId") == desc["limitId"]]
        if len(current) != 1:
            raise DataNotAvailable("no limit data")
        if current[0].get("isLimitChangeable") is False:
            raise DataNotAvailable("the limit is not changeable")
        item: dict[str, Any] = {"limitId": desc["limitId"], "isLimitActive": limit.is_active,
                                "value": scaled_number(limit.value)}
        if limit.duration and limit.duration > 0:
            item["timePeriod"] = {"endTime": format_duration(limit.duration)}
        filters = []
        if limit.delete_duration:
            filters.append(delete_filter({"limitId": desc["limitId"]}, FN_LIMITS, {"timePeriod": {}}))
        filters.append(partial_filter())
        ops = f.remote.operations.get(FN_LIMITS)
        if ops is not None and not ops.write_partial:
            merged = {i.get("limitId"): dict(i) for i in f.items(FN_LIMITS, "loadControlLimitData")}
            merged.setdefault(item["limitId"], {}).update(item)
            await f.write(FN_LIMITS, {"loadControlLimitData": list(merged.values())},
                                         None, timeout)
        else:
            await f.write(FN_LIMITS, {"loadControlLimitData": [item]}, filters, timeout)

    # failsafe values (device configuration)
    def _config(self, entity: RemoteEntity, key: str) -> tuple[ClientFeature, dict, dict | None]:
        f = ClientFeature.find(DEVICE_CONFIGURATION, self.local, entity)
        if f is None:
            raise DataNotAvailable("no device configuration feature")
        descs = filter_items(f.items(FN_CONFIG_DESCRIPTIONS, "deviceConfigurationKeyValueDescriptionData"),
                             keyName=key)
        if len(descs) != 1:
            raise DataNotAvailable(f"no {key} description")
        value = next((i for i in f.items(FN_CONFIG_VALUES, "deviceConfigurationKeyValueData")
                      if i.get("keyId") == descs[0].get("keyId")), None)
        return f, descs[0], value

    def failsafe_consumption_limit(self, entity: RemoteEntity) -> float:
        """Failsafe active power limit (W) used when the energy guard is gone."""
        _f, _desc, value = self._config(entity, KEY_FAILSAFE_POWER)
        number = ((value or {}).get("value") or {}).get("scaledNumber")
        if scaled_value(number) is None:
            raise DataNotAvailable("no failsafe limit")
        return scaled_value(number)

    def failsafe_duration_minimum(self, entity: RemoteEntity) -> float:
        """Minimum failsafe duration in seconds."""
        _f, _desc, value = self._config(entity, KEY_FAILSAFE_DURATION)
        duration = ((value or {}).get("value") or {}).get("duration")
        if not duration:
            raise DataNotAvailable("no failsafe duration")
        return parse_duration(duration)

    async def _write_config(self, entity: RemoteEntity, key: str, value: dict[str, Any],
                            timeout: float | None) -> None:
        f, desc, _current = self._config(entity, key)
        item = {"keyId": desc["keyId"], "value": value}
        ops = f.remote.operations.get(FN_CONFIG_VALUES)
        if ops is not None and ops.write_partial:
            await f.write(FN_CONFIG_VALUES, {"deviceConfigurationKeyValueData": [item]},
                                         [partial_filter()], timeout)
        else:
            merged = {i.get("keyId"): dict(i) for i in f.items(FN_CONFIG_VALUES, "deviceConfigurationKeyValueData")}
            merged.setdefault(item["keyId"], {}).update(item)
            await f.write(FN_CONFIG_VALUES,
                                         {"deviceConfigurationKeyValueData": list(merged.values())}, None, timeout)

    async def write_failsafe_consumption_limit(self, entity: RemoteEntity, watts: float,
                                               timeout: float | None = None) -> None:
        await self._write_config(entity, KEY_FAILSAFE_POWER, {"scaledNumber": scaled_number(watts)}, timeout)

    async def write_failsafe_duration_minimum(self, entity: RemoteEntity, seconds: float,
                                              timeout: float | None = None) -> None:
        if not 2 * 3600 <= seconds <= 24 * 3600:
            raise ValueError("failsafe duration must be between 2 and 24 hours")
        await self._write_config(entity, KEY_FAILSAFE_DURATION, {"duration": format_duration(seconds)}, timeout)

    # heartbeat and nominal power
    def is_heartbeat_within(self, entity: RemoteEntity, seconds: float = 120) -> bool:
        f = ClientFeature.find(DEVICE_DIAGNOSIS, self.local, entity)
        data = f.data(FN_HEARTBEAT) if f else None
        if not data or not data.get("timestamp"):
            return False
        try:
            age = (utcnow() - parse_datetime(data["timestamp"])).total_seconds()
        except ValueError:
            return False
        return age <= seconds

    def consumption_nominal_max(self, entity: RemoteEntity) -> float:
        """Maximum power the device can consume (W)."""
        f = ClientFeature.find(ELECTRICAL_CONNECTION, self.local, entity)
        if f is None:
            raise DataNotAvailable("no electrical connection feature")
        items = filter_items(f.items(FN_EC_CHARACTERISTICS, "electricalConnectionCharacteristicData"),
                             characteristicContext="entity", characteristicType=self._characteristic_type(entity))
        if not items or scaled_value(items[0].get("value")) is None:
            raise DataNotAvailable("no nominal maximum")
        return scaled_value(items[0]["value"])


# --- MPC -----------------------------------------------------------------------------------


class MPC(UseCase):
    actor = "MonitoringAppliance"
    name = "monitoringOfPowerConsumption"
    version = "1.0.0"
    scenarios = (Scenario(1, True, (ELECTRICAL_CONNECTION, MEASUREMENT)),
                 *(Scenario(i, False, (ELECTRICAL_CONNECTION, MEASUREMENT)) for i in (2, 3, 4, 5)))
    valid_actors = ("MonitoredUnit",)
    valid_entity_types = ("Compressor", "ElectricalImmersionHeater", "EVSE", "HeatPumpAppliance", "Inverter",
                          "SmartEnergyAppliance", "SubMeterElectricity")
    client_features = (ELECTRICAL_CONNECTION, MEASUREMENT)

    DATA_UPDATE_POWER = "DataUpdatePower"
    DATA_UPDATE_POWER_PER_PHASE = "DataUpdatePowerPerPhase"
    DATA_UPDATE_ENERGY_CONSUMED = "DataUpdateEnergyConsumed"
    DATA_UPDATE_ENERGY_PRODUCED = "DataUpdateEnergyProduced"
    DATA_UPDATE_CURRENTS_PER_PHASE = "DataUpdateCurrentsPerPhase"
    DATA_UPDATE_VOLTAGE_PER_PHASE = "DataUpdateVoltagePerPhase"
    DATA_UPDATE_FREQUENCY = "DataUpdateFrequency"

    _SCOPE_EVENTS = (("acPowerTotal", DATA_UPDATE_POWER), ("acPower", DATA_UPDATE_POWER_PER_PHASE),
                     ("acEnergyConsumed", DATA_UPDATE_ENERGY_CONSUMED),
                     ("acEnergyProduced", DATA_UPDATE_ENERGY_PRODUCED),
                     ("acCurrent", DATA_UPDATE_CURRENTS_PER_PHASE), ("acVoltage", DATA_UPDATE_VOLTAGE_PER_PHASE),
                     ("acFrequency", DATA_UPDATE_FREQUENCY))

    def handle_entity_event(self, event: Event) -> None:
        entity = event.entity
        if self.is_entity_added(event):
            if f := ClientFeature.find(ELECTRICAL_CONNECTION, self.local, entity):
                f.subscribe()
                f.request(FN_EC_DESCRIPTIONS, FN_EC_PARAMETERS)
            if f := ClientFeature.find(MEASUREMENT, self.local, entity):
                f.subscribe()
                f.request(FN_MEAS_DESCRIPTIONS, FN_MEAS_CONSTRAINTS)
        elif self.is_data_update(event, FN_MEAS_DESCRIPTIONS):
            if f := ClientFeature.find(MEASUREMENT, self.local, entity):
                f.request(FN_MEASUREMENTS)
        elif self.is_data_update(event, FN_MEASUREMENTS):
            f = ClientFeature.find(MEASUREMENT, self.local, entity)
            if f is None:
                return
            descs = f.items(FN_MEAS_DESCRIPTIONS, "measurementDescriptionData")
            ids = {m.get("measurementId") for m in as_list((event.data or {}).get("measurementData"))}
            scopes = {d.get("scopeType") for d in descs if d.get("measurementId") in ids}
            for scope, name in self._SCOPE_EVENTS:
                if scope in scopes:
                    self._emit(event.ski, entity, name)

    def _values(self, entity: RemoteEntity, description: dict[str, Any], direction: str | None,
                phases: tuple[str, ...] | None) -> list[float]:
        meas = ClientFeature.find(MEASUREMENT, self.local, entity)
        ec = ClientFeature.find(ELECTRICAL_CONNECTION, self.local, entity)
        if meas is None or ec is None:
            raise DataNotAvailable("no measurement or electrical connection feature")
        descs = filter_items(meas.items(FN_MEAS_DESCRIPTIONS, "measurementDescriptionData"), description)
        ids = [d.get("measurementId") for d in descs]
        data = [m for m in meas.items(FN_MEASUREMENTS, "measurementData") if m.get("measurementId") in ids]
        if not data:
            raise DataNotAvailable("no matching measurements")
        params = ec.items(FN_EC_PARAMETERS, "electricalConnectionParameterDescriptionData")
        connections = ec.items(FN_EC_DESCRIPTIONS, "electricalConnectionDescriptionData")
        values = []
        by_phase: dict[str, float] = {}
        for item in data:
            value = scaled_value(item.get("value"))
            if value is None:
                continue
            param = next((p for p in params if p.get("measurementId") == item.get("measurementId")), None)
            if phases is not None and (param is None or param.get("acMeasuredPhases") not in phases):
                continue
            if direction:
                connection = next((c for c in connections if param is not None and
                                   c.get("electricalConnectionId") == param.get("electricalConnectionId")), None)
                if connection is None:
                    continue
                if connection.get("positiveEnergyDirection") != direction:
                    raise DataNotAvailable("unexpected energy direction")
            if item.get("valueState") not in (None, "normal"):
                raise DataInvalid(f"value state {item['valueState']}")
            if phases is not None:
                by_phase[param["acMeasuredPhases"]] = value
            values.append(value)
        if phases is not None:
            return [by_phase[p] for p in phases if p in by_phase]
        return values

    def _single(self, entity: RemoteEntity, **description: Any) -> float:
        values = self._values(entity, {"commodityType": "electricity", **description}, None, None)
        if not values:
            raise DataNotAvailable("no value")
        return values[0]

    def power(self, entity: RemoteEntity) -> float:
        """Total active power in W (positive: consumption)."""
        values = self._values(entity, {"measurementType": "power", "commodityType": "electricity",
                                       "scopeType": "acPowerTotal"}, "consume", None)
        if len(values) != 1:
            raise DataNotAvailable("no total power")
        return values[0]

    def power_per_phase(self, entity: RemoteEntity) -> list[float]:
        return self._values(entity, {"measurementType": "power", "commodityType": "electricity",
                                     "scopeType": "acPower"}, "consume", PHASES)

    def energy_consumed(self, entity: RemoteEntity) -> float:
        """Consumed energy in Wh."""
        return self._single(entity, measurementType="energy", scopeType="acEnergyConsumed")

    def energy_produced(self, entity: RemoteEntity) -> float:
        return self._single(entity, measurementType="energy", scopeType="acEnergyProduced")

    def current_per_phase(self, entity: RemoteEntity) -> list[float]:
        return self._values(entity, {"measurementType": "current", "commodityType": "electricity",
                                     "scopeType": "acCurrent"}, "consume", PHASES)

    def voltage_per_phase(self, entity: RemoteEntity) -> list[float]:
        return self._values(entity, {"measurementType": "voltage", "commodityType": "electricity",
                                     "scopeType": "acVoltage"}, None, PHASES)

    def frequency(self, entity: RemoteEntity) -> float:
        return self._single(entity, measurementType="frequency", scopeType="acFrequency")
