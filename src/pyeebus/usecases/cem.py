"""CEM use cases for EV charging (port of eebus-go's usecases/cem).

* :class:`EVSECC` EVSE commissioning and configuration
* :class:`EVCC` EV commissioning and configuration
* :class:`EVCEM` measurement of electricity during EV charging
* :class:`OPEV` overload protection by EV charging current curtailment
* :class:`OSCEV` optimization of self consumption during EV charging
* :class:`EVSOC` EV state of charge
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..spine.device import Event, RemoteEntity, partial_filter
from ..spine.model import Role, as_list, filter_items, scaled_number, scaled_value
from .base import (
    DEVICE_CLASSIFICATION,
    DEVICE_CONFIGURATION,
    DEVICE_DIAGNOSIS,
    ELECTRICAL_CONNECTION,
    IDENTIFICATION,
    LOAD_CONTROL,
    MEASUREMENT,
    PHASES,
    ClientFeature,
    DataNotAvailable,
    Scenario,
    UseCase,
    adjust_to_permitted,
    configuration_value,
    diagnosis_state,
    manufacturer_data,
    measurements,
    parameter_descriptions,
    permitted_values,
)

FN_MANUFACTURER = "deviceClassificationManufacturerData"
FN_DIAGNOSIS_STATE = "deviceDiagnosisStateData"
FN_DIAGNOSIS_HEARTBEAT = "deviceDiagnosisHeartbeatData"
FN_CONFIG_DESCRIPTIONS = "deviceConfigurationKeyValueDescriptionListData"
FN_CONFIG_VALUES = "deviceConfigurationKeyValueListData"
FN_EC_DESCRIPTIONS = "electricalConnectionDescriptionListData"
FN_EC_PARAMETERS = "electricalConnectionParameterDescriptionListData"
FN_EC_PERMITTED = "electricalConnectionPermittedValueSetListData"
FN_IDENTIFICATION = "identificationListData"
FN_LIMIT_DESCRIPTIONS = "loadControlLimitDescriptionListData"
FN_LIMIT_CONSTRAINTS = "loadControlLimitConstraintsListData"
FN_LIMITS = "loadControlLimitListData"
FN_MEAS_DESCRIPTIONS = "measurementDescriptionListData"
FN_MEAS_CONSTRAINTS = "measurementConstraintsListData"
FN_MEASUREMENTS = "measurementListData"


def _add_diagnosis_server(uc: UseCase) -> None:
    feature = uc.local.add_feature(DEVICE_DIAGNOSIS, Role.SERVER)
    feature.add_function(FN_DIAGNOSIS_STATE, read=True)
    if FN_DIAGNOSIS_STATE not in feature.data:
        feature.data[FN_DIAGNOSIS_STATE] = {"operatingState": "normalOperation"}
    feature.add_function(FN_DIAGNOSIS_HEARTBEAT, read=True)


def set_operating_state(uc: UseCase, failure: bool) -> None:
    feature = uc.local.feature(DEVICE_DIAGNOSIS, Role.SERVER)
    if feature is not None:
        feature.set_data(FN_DIAGNOSIS_STATE, {"operatingState": "failure" if failure else "normalOperation"})


# --- EVSECC ------------------------------------------------------------------------------


class EVSECC(UseCase):
    name = "evseCommissioningAndConfiguration"
    version = "1.0.1"
    scenarios = (Scenario(1, server_features=(DEVICE_CLASSIFICATION,)),
                 Scenario(2, True, (DEVICE_DIAGNOSIS,)))
    valid_actors = ("EVSE", "EV")  # Porsche PMCC uses EV here
    valid_entity_types = ("EVSE",)
    client_features = (DEVICE_CLASSIFICATION, DEVICE_DIAGNOSIS)

    EVSE_CONNECTED = "EvseConnected"
    EVSE_DISCONNECTED = "EvseDisconnected"
    DATA_UPDATE_MANUFACTURER_DATA = "DataUpdateManufacturerData"
    DATA_UPDATE_OPERATING_STATE = "DataUpdateOperatingState"

    def handle_entity_event(self, event: Event) -> None:
        if self.is_entity_added(event):
            if f := ClientFeature.find(DEVICE_CLASSIFICATION, self.local, event.entity):
                f.request(FN_MANUFACTURER)
            if f := ClientFeature.find(DEVICE_DIAGNOSIS, self.local, event.entity):
                f.subscribe()
                f.request(FN_DIAGNOSIS_STATE)
            self._emit(event.ski, event.entity, self.EVSE_CONNECTED)
        elif self.is_entity_removed(event):
            self._emit(event.ski, event.entity, self.EVSE_DISCONNECTED)
        elif self.is_data_update(event, FN_MANUFACTURER):
            self._emit(event.ski, event.entity, self.DATA_UPDATE_MANUFACTURER_DATA)
        elif self.is_data_update(event, FN_DIAGNOSIS_STATE):
            self._emit(event.ski, event.entity, self.DATA_UPDATE_OPERATING_STATE)

    def manufacturer_data(self, entity: RemoteEntity) -> dict[str, Any]:
        return manufacturer_data(self.local, entity)

    def operating_state(self, entity: RemoteEntity) -> tuple[str, str]:
        """(operatingState, lastErrorCode)"""
        state = diagnosis_state(self.local, entity)
        return state.get("operatingState", "normalOperation"), state.get("lastErrorCode", "")


# --- EVCC ---------------------------------------------------------------------------------


class EVCC(UseCase):
    name = "evCommissioningAndConfiguration"
    version = "1.0.1"
    scenarios = (
        Scenario(1, True), Scenario(2, True, (DEVICE_CONFIGURATION,)),
        Scenario(3, True, (DEVICE_CONFIGURATION,)), Scenario(4, server_features=(IDENTIFICATION,)),
        Scenario(5, server_features=(DEVICE_CLASSIFICATION,)),
        Scenario(6, server_features=(ELECTRICAL_CONNECTION,)),
        Scenario(7, server_features=(DEVICE_DIAGNOSIS,)), Scenario(8, True))
    valid_actors = ("EV",)
    valid_entity_types = ("EV",)
    client_features = (DEVICE_CONFIGURATION, IDENTIFICATION, DEVICE_CLASSIFICATION,
                       ELECTRICAL_CONNECTION, DEVICE_DIAGNOSIS)

    EV_CONNECTED = "EvConnected"
    EV_DISCONNECTED = "EvDisconnected"
    DATA_UPDATE_COMMUNICATION_STANDARD = "DataUpdateCommunicationStandard"
    DATA_UPDATE_ASYMMETRIC_CHARGING = "DataUpdateAsymmetricChargingSupport"
    DATA_UPDATE_IDENTIFICATIONS = "DataUpdateIdentifications"
    DATA_UPDATE_MANUFACTURER_DATA = "DataUpdateManufacturerData"
    DATA_UPDATE_CURRENT_LIMITS = "DataUpdateCurrentLimits"
    DATA_UPDATE_OPERATING_STATE = "DataUpdateOperatingState"

    def handle_entity_event(self, event: Event) -> None:
        entity = event.entity
        if self.is_entity_added(event):
            if f := ClientFeature.find(DEVICE_CLASSIFICATION, self.local, entity):
                f.subscribe()
                f.request(FN_MANUFACTURER)
            if f := ClientFeature.find(DEVICE_CONFIGURATION, self.local, entity):
                f.subscribe()
                f.request(FN_CONFIG_DESCRIPTIONS)
            if f := ClientFeature.find(DEVICE_DIAGNOSIS, self.local, entity):
                f.subscribe()
                f.request(FN_DIAGNOSIS_STATE)
            if f := ClientFeature.find(ELECTRICAL_CONNECTION, self.local, entity):
                f.subscribe()
                f.request(FN_EC_PARAMETERS, FN_EC_PERMITTED)
            if f := ClientFeature.find(IDENTIFICATION, self.local, entity):
                f.subscribe()
                f.request(FN_IDENTIFICATION)
            self._emit(event.ski, entity, self.EV_CONNECTED)
        elif self.is_entity_removed(event):
            self._emit(event.ski, entity, self.EV_DISCONNECTED)
        elif self.is_data_update(event, FN_CONFIG_DESCRIPTIONS):
            if f := ClientFeature.find(DEVICE_CONFIGURATION, self.local, entity):
                f.request(FN_CONFIG_VALUES)
        elif self.is_data_update(event, FN_CONFIG_VALUES):
            self._emit(event.ski, entity, self.DATA_UPDATE_COMMUNICATION_STANDARD)
            self._emit(event.ski, entity, self.DATA_UPDATE_ASYMMETRIC_CHARGING)
        elif self.is_data_update(event, FN_DIAGNOSIS_STATE):
            self._emit(event.ski, entity, self.DATA_UPDATE_OPERATING_STATE)
        elif self.is_data_update(event, FN_MANUFACTURER):
            self._emit(event.ski, entity, self.DATA_UPDATE_MANUFACTURER_DATA)
        elif self.is_data_update(event, FN_EC_PARAMETERS):
            if f := ClientFeature.find(ELECTRICAL_CONNECTION, self.local, entity):
                f.request(FN_EC_PERMITTED)
        elif self.is_data_update(event, FN_EC_PERMITTED):
            self._emit(event.ski, entity, self.DATA_UPDATE_CURRENT_LIMITS)
        elif self.is_data_update(event, FN_IDENTIFICATION):
            self._emit(event.ski, entity, self.DATA_UPDATE_IDENTIFICATIONS)

    def charge_state(self, entity: RemoteEntity | None) -> str:
        """unplugged, active, paused, error, finished or unknown"""
        if entity is None or entity.type != "EV":
            return "unplugged"
        try:
            state = diagnosis_state(self.local, entity).get("operatingState")
        except DataNotAvailable:
            return "unknown"
        return {"normalOperation": "active", "standby": "paused", "failure": "error",
                "finished": "finished"}.get(state or "", "unknown")

    def ev_connected(self, entity: RemoteEntity | None) -> bool:
        return (entity is not None and entity.device.entity(entity.entity) is entity
                and self.charge_state(entity) != "unknown")

    def communication_standard(self, entity: RemoteEntity) -> str:
        """iso15118-2ed1, iso15118-2ed2, iec61851 or unknown"""
        try:
            return configuration_value(self.local, entity, "communicationsStandard", "string")
        except DataNotAvailable:
            return "unknown"

    def asymmetric_charging_support(self, entity: RemoteEntity) -> bool:
        return bool(configuration_value(self.local, entity, "asymmetricChargingSupported", "boolean"))

    def identifications(self, entity: RemoteEntity) -> list[tuple[str, str]]:
        """[(identificationType, identificationValue)], e.g. the EV's MAC address"""
        f = ClientFeature.find(IDENTIFICATION, self.local, entity)
        if f is None:
            raise DataNotAvailable("no identification feature")
        return [(i.get("identificationType", ""), str(i.get("identificationValue", "")))
                for i in f.items(FN_IDENTIFICATION, "identificationData")]

    def manufacturer_data(self, entity: RemoteEntity) -> dict[str, Any]:
        return manufacturer_data(self.local, entity)

    def charging_power_limits(self, entity: RemoteEntity) -> tuple[float, float, float]:
        """(min, max, standby) power in W"""
        params = parameter_descriptions(self.local, entity, scopeType="acPowerTotal")
        if not params or params[0].get("parameterId") is None:
            raise DataNotAvailable("no power limits")
        low, high, standby = permitted_values(self.local, entity, params[0]["parameterId"])
        return low or 0.0, high or 0.0, standby or 0.0

    def is_in_sleep_mode(self, entity: RemoteEntity) -> bool:
        return diagnosis_state(self.local, entity).get("operatingState") == "standby"


# --- EVCEM ----------------------------------------------------------------------------------


class EVCEM(UseCase):
    name = "measurementOfElectricityDuringEvCharging"
    version = "1.0.1"
    scenarios = tuple(Scenario(i, server_features=(ELECTRICAL_CONNECTION, MEASUREMENT)) for i in (1, 2, 3))
    valid_actors = ("EV",)
    valid_entity_types = ("EV",)
    client_features = (ELECTRICAL_CONNECTION, MEASUREMENT)

    DATA_UPDATE_PHASES_CONNECTED = "DataUpdatePhasesConnected"
    DATA_UPDATE_CURRENT_PER_PHASE = "DataUpdateCurrentPerPhase"
    DATA_UPDATE_POWER_PER_PHASE = "DataUpdatePowerPerPhase"
    DATA_UPDATE_ENERGY_CHARGED = "DataUpdateEnergyCharged"

    def handle_entity_event(self, event: Event) -> None:
        entity = event.entity
        if self.is_entity_added(event):
            if f := ClientFeature.find(ELECTRICAL_CONNECTION, self.local, entity):
                f.subscribe()
                f.request(FN_EC_DESCRIPTIONS, FN_EC_PARAMETERS)
            if f := ClientFeature.find(MEASUREMENT, self.local, entity):
                f.subscribe()
                f.request(FN_MEAS_DESCRIPTIONS, FN_MEAS_CONSTRAINTS)
        elif self.is_data_update(event, FN_EC_DESCRIPTIONS):
            if any(i.get("acConnectedPhases") is not None
                   for i in as_list((event.data or {}).get("electricalConnectionDescriptionData"))):
                self._emit(event.ski, entity, self.DATA_UPDATE_PHASES_CONNECTED)
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
            for scope, name in (("acCurrent", self.DATA_UPDATE_CURRENT_PER_PHASE),
                                ("acPower", self.DATA_UPDATE_POWER_PER_PHASE),
                                ("charge", self.DATA_UPDATE_ENERGY_CHARGED)):
                if scope in scopes:
                    self._emit(event.ski, entity, name)

    def phases_connected(self, entity: RemoteEntity) -> int:
        f = ClientFeature.find(ELECTRICAL_CONNECTION, self.local, entity)
        if f is None:
            raise DataNotAvailable("no electrical connection feature")
        for item in f.items(FN_EC_DESCRIPTIONS, "electricalConnectionDescriptionData"):
            if item.get("electricalConnectionId") is not None and item.get("acConnectedPhases") is not None:
                return int(item["acConnectedPhases"])
        return 0

    def _per_phase(self, entity: RemoteEntity, **description: Any) -> list[float]:
        data = measurements(self.local, entity, commodityType="electricity", **description)
        params = parameter_descriptions(self.local, entity)
        phase_of = {p.get("measurementId"): p.get("acMeasuredPhases") for p in params}
        result = []
        for phase in PHASES:
            for item in data:
                value = scaled_value(item.get("value"))
                if value is not None and phase_of.get(item.get("measurementId")) == phase:
                    result.append(value)
        return result

    def current_per_phase(self, entity: RemoteEntity) -> list[float]:
        """Charging current per phase in A"""
        return self._per_phase(entity, measurementType="current", scopeType="acCurrent")

    def power_per_phase(self, entity: RemoteEntity) -> list[float]:
        """Charging power per phase in W"""
        return self._per_phase(entity, measurementType="power", scopeType="acPower")

    def energy_charged(self, entity: RemoteEntity) -> float:
        """Energy charged in this session in Wh"""
        data = measurements(self.local, entity, measurementType="energy", commodityType="electricity",
                            scopeType="charge")
        value = scaled_value(data[0].get("value"))
        if value is None:
            raise DataNotAvailable("no energy value")
        return value


# --- OPEV / OSCEV ---------------------------------------------------------------------------


@dataclass
class PhaseLimit:
    phase: str
    value: float
    is_active: bool = True
    is_changeable: bool = True


class _LimitUseCase(UseCase):
    """Shared logic of OPEV (obligation) and OSCEV (recommendation)."""

    valid_actors = ("EV",)
    valid_entity_types = ("EV",)
    client_features = (LOAD_CONTROL, ELECTRICAL_CONNECTION)
    limit_category = ""
    limit_scope = ""

    DATA_UPDATE_LIMIT = "DataUpdateLimit"
    DATA_UPDATE_CURRENT_LIMITS = "DataUpdateCurrentLimits"

    @property
    def _limit_filter(self) -> dict[str, Any]:
        return {"limitType": "maxValueLimit", "limitCategory": self.limit_category,
                "unit": "A", "scopeType": self.limit_scope}

    def add_features(self) -> None:
        super().add_features()
        _add_diagnosis_server(self)

    def handle_entity_event(self, event: Event) -> None:
        entity = event.entity
        if self.is_data_update(event, FN_EC_PERMITTED):
            self._emit(event.ski, entity, self.DATA_UPDATE_CURRENT_LIMITS)
        elif self.is_data_update(event, FN_LIMITS):
            f = ClientFeature.find(LOAD_CONTROL, self.local, entity)
            if f is None:
                return
            descs = filter_items(f.items(FN_LIMIT_DESCRIPTIONS, "loadControlLimitDescriptionData"),
                                 {k: v for k, v in self._limit_filter.items() if k != "unit"})
            ids = {d.get("limitId") for d in descs}
            if any(i.get("limitId") in ids for i in as_list((event.data or {}).get("loadControlLimitData"))):
                self._emit(event.ski, entity, self.DATA_UPDATE_LIMIT)

    def _limit_descriptions(self, entity: RemoteEntity) -> list[dict[str, Any]]:
        f = ClientFeature.find(LOAD_CONTROL, self.local, entity)
        if f is None:
            raise DataNotAvailable("no load control feature")
        descs = filter_items(f.items(FN_LIMIT_DESCRIPTIONS, "loadControlLimitDescriptionData"),
                             self._limit_filter)
        if not descs:
            raise DataNotAvailable("no limit descriptions")
        return descs

    def _phase_params(self, entity: RemoteEntity) -> dict[str, tuple[dict, dict]]:
        """phase -> (parameter description, limit description)"""
        descs = self._limit_descriptions(entity)
        out = {}
        for phase in PHASES:
            for param in parameter_descriptions(self.local, entity, acMeasuredPhases=phase):
                if param.get("measurementId") is None:
                    continue
                limit = next((d for d in descs if d.get("measurementId") == param["measurementId"]), None)
                if limit is not None:
                    out[phase] = (param, limit)
                    break
        return out

    def current_limits(self, entity: RemoteEntity) -> tuple[list[float], list[float], list[float]]:
        """Per phase (min, max, default) charging current in A"""
        lows, highs, defaults = [], [], []
        for param, _limit in self._phase_params(entity).values():
            try:
                low, high, default = permitted_values(self.local, entity, param["parameterId"])
            except (DataNotAvailable, KeyError):
                continue
            lows.append(low or 0.0)
            highs.append(high or 0.0)
            defaults.append(default or 0.0)
        if not lows:
            raise DataNotAvailable("no current limits")
        return lows, highs, defaults

    def load_control_limits(self, entity: RemoteEntity) -> list[PhaseLimit]:
        f = ClientFeature.find(LOAD_CONTROL, self.local, entity)
        if f is None:
            raise DataNotAvailable("no load control feature")
        data = f.items(FN_LIMITS, "loadControlLimitData")
        result = []
        for phase, (param, desc) in self._phase_params(entity).items():
            item = next((i for i in data if i.get("limitId") == desc.get("limitId")), None)
            if item is None:
                raise DataNotAvailable("no limit data")
            value = scaled_value(item.get("value"))
            if value is None or item.get("isLimitActive") is False:
                try:
                    value = permitted_values(self.local, entity, param.get("parameterId"))[1]
                except DataNotAvailable:
                    continue
            result.append(PhaseLimit(phase, value or 0.0, bool(item.get("isLimitActive")),
                                     bool(item.get("isLimitChangeable"))))
        if not result:
            raise DataNotAvailable("no limits")
        return result

    async def write_load_control_limits(self, entity: RemoteEntity, limits: list[PhaseLimit],
                                        timeout: float | None = None) -> None:
        """Write per phase current limits (A); raises SpineError if the EVSE rejects them."""
        f = ClientFeature.find(LOAD_CONTROL, self.local, entity)
        if f is None:
            raise DataNotAvailable("no load control feature")
        params = self._phase_params(entity)
        current = f.items(FN_LIMITS, "loadControlLimitData")
        data = []
        for limit in limits:
            if limit.phase not in params:
                continue
            param, desc = params[limit.phase]
            existing = next((i for i in current if i.get("limitId") == desc.get("limitId")), {})
            if existing.get("isLimitChangeable") is False:
                continue
            value = adjust_to_permitted(self.local, entity, limit.value, param.get("parameterId"))
            data.append({"limitId": desc["limitId"], "isLimitActive": limit.is_active,
                         "value": scaled_number(value)})
        if not data:
            raise DataNotAvailable("no writable limits")
        ops = f.remote.operations.get(FN_LIMITS)
        if ops is not None and ops.write_partial:
            await f.local.write_and_wait(f.remote, FN_LIMITS, {"loadControlLimitData": data},
                                         [partial_filter()], timeout)
        else:
            # full write: merge into the known list
            merged = {i.get("limitId"): dict(i) for i in current}
            for item in data:
                merged.setdefault(item["limitId"], {}).update(item)
            await f.local.write_and_wait(f.remote, FN_LIMITS,
                                         {"loadControlLimitData": list(merged.values())}, None, timeout)

    def set_operating_state(self, failure: bool) -> None:
        set_operating_state(self, failure)


class OPEV(_LimitUseCase):
    name = "overloadProtectionByEvChargingCurrentCurtailment"
    version = "1.0.1"
    scenarios = (Scenario(1, True, (LOAD_CONTROL, ELECTRICAL_CONNECTION)), Scenario(2, True),
                 Scenario(3, True))
    limit_category = "obligation"
    limit_scope = "overloadProtection"

    def handle_entity_event(self, event: Event) -> None:
        entity = event.entity
        if self.is_entity_added(event):
            if f := ClientFeature.find(LOAD_CONTROL, self.local, entity):
                f.subscribe()
                f.bind()
                f.request(FN_LIMIT_DESCRIPTIONS, FN_LIMIT_CONSTRAINTS)
        elif self.is_data_update(event, FN_LIMIT_DESCRIPTIONS):
            if f := ClientFeature.find(LOAD_CONTROL, self.local, entity):
                f.request(FN_LIMITS)
        else:
            super().handle_entity_event(event)


class OSCEV(_LimitUseCase):
    name = "optimizationOfSelfConsumptionDuringEvCharging"
    version = "1.0.1"
    scenarios = (Scenario(1, True, (LOAD_CONTROL, ELECTRICAL_CONNECTION)), Scenario(2, True),
                 Scenario(3, True))
    limit_category = "recommendation"
    limit_scope = "selfConsumption"


# --- EVSOC ------------------------------------------------------------------------------------


class EVSOC(UseCase):
    name = "evStateOfCharge"
    version = "1.0.0"
    scenarios = (Scenario(1, True, (MEASUREMENT,)),)
    valid_actors = ("EV",)
    valid_entity_types = ("EV",)
    client_features = (ELECTRICAL_CONNECTION, MEASUREMENT)

    DATA_UPDATE_STATE_OF_CHARGE = "DataUpdateStateOfCharge"

    def handle_entity_event(self, event: Event) -> None:
        if self.is_data_update(event, FN_MEASUREMENTS):
            try:
                self.state_of_charge(event.entity)
            except DataNotAvailable:
                return
            self._emit(event.ski, event.entity, self.DATA_UPDATE_STATE_OF_CHARGE)

    def state_of_charge(self, entity: RemoteEntity) -> float:
        data = measurements(self.local, entity, scopeType="stateOfCharge")
        value = scaled_value(data[0].get("value"))
        if value is None:
            raise DataNotAvailable("no state of charge")
        return value


ALL_EV_USE_CASES = (EVSECC, EVCC, EVCEM, OPEV, OSCEV, EVSOC)

__all__ = ["ALL_EV_USE_CASES", "EVCC", "EVCEM", "EVSECC", "EVSOC", "OPEV", "OSCEV", "PhaseLimit"]
