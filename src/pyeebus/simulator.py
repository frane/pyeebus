"""A simulated EEBUS wallbox (EVSE) with a pluggable EV, for tests and development.

It offers what a CEM expects from an EVSE (EVSECC) and an EV (EVCC, EVCEM,
OPEV, OSCEV): manufacturer data, operating state, electrical connection
parameters with permitted values, measurements and writable current limits.
"""

from __future__ import annotations

import logging
from typing import Any

from .service import EebusService
from .ship import Identity, TrustStore
from .spine.device import LocalEntity, Message
from .spine.model import Role, scaled_number, scaled_value

_LOGGER = logging.getLogger(__name__)

PHASES = ("a", "b", "c")


def _sn(value: float) -> dict[str, int]:
    return scaled_number(value)


class SimulatedEVSE:
    """EVSE entity ``[1]`` and, while plugged, EV entity ``[1, 1]``."""

    def __init__(self, identity: Identity, *, port: int = 4712, trust: TrustStore | None = None,
                 min_current: float = 6.0, max_current: float = 16.0, phases: int = 3,
                 **node_options: Any) -> None:
        self.service = EebusService(
            identity, brand="pyeebus", model="SimEVSE", serial="0001", device_type="ChargingStation",
            entity_types=("EVSE",), port=port, trust=trust, **node_options)
        self.min_current, self.max_current, self.phases = min_current, max_current, phases
        self.evse: LocalEntity = self.service.entities[0]
        self.ev: LocalEntity | None = None
        self.limits: dict[str, list[float]] = {"obligation": [max_current] * 3,
                                               "recommendation": [max_current] * 3}
        self._setup_evse()
        self.device.subscribe_events(self._on_event)

    @property
    def device(self):
        return self.service.device

    def _setup_evse(self) -> None:
        f = self.evse.add_feature("DeviceClassification", Role.SERVER)
        f.add_function("deviceClassificationManufacturerData")
        f.data["deviceClassificationManufacturerData"] = {
            "deviceName": "SimEVSE", "deviceCode": "sim-evse", "serialNumber": "0001",
            "softwareRevision": "1.0", "vendorName": "pyeebus", "brandName": "pyeebus"}
        f = self.evse.add_feature("DeviceDiagnosis", Role.SERVER)
        f.add_function("deviceDiagnosisStateData")
        f.data["deviceDiagnosisStateData"] = {"operatingState": "normalOperation"}
        self.evse.add_use_case("EVSE", "evseCommissioningAndConfiguration", "1.0.1", [1, 2])

    # --- EV -----------------------------------------------------------------------------

    def plug_in(self, communication_standard: str = "iec61851") -> LocalEntity:
        if self.ev is not None:
            return self.ev
        ev = self.device.add_entity("EV", (*self.evse.entity, 1), announce=False)
        self.ev = ev
        phases = PHASES[: self.phases]

        f = ev.add_feature("DeviceClassification", Role.SERVER)
        f.add_function("deviceClassificationManufacturerData")
        f.data["deviceClassificationManufacturerData"] = {"deviceName": "SimEV", "brandName": "pyeebus"}

        f = ev.add_feature("DeviceDiagnosis", Role.SERVER)
        f.add_function("deviceDiagnosisStateData")
        f.data["deviceDiagnosisStateData"] = {"operatingState": "normalOperation"}

        f = ev.add_feature("DeviceConfiguration", Role.SERVER)
        f.add_function("deviceConfigurationKeyValueDescriptionListData")
        f.add_function("deviceConfigurationKeyValueListData")
        f.data["deviceConfigurationKeyValueDescriptionListData"] = {"deviceConfigurationKeyValueDescriptionData": [
            {"keyId": 1, "keyName": "communicationsStandard", "valueType": "string"},
            {"keyId": 2, "keyName": "asymmetricChargingSupported", "valueType": "boolean"}]}
        f.data["deviceConfigurationKeyValueListData"] = {"deviceConfigurationKeyValueData": [
            {"keyId": 1, "value": {"string": communication_standard}, "isValueChangeable": False},
            {"keyId": 2, "value": {"boolean": False}, "isValueChangeable": False}]}

        f = ev.add_feature("Identification", Role.SERVER)
        f.add_function("identificationListData")
        f.data["identificationListData"] = {"identificationData": [
            {"identificationId": 0, "identificationType": "eui48", "identificationValue": "02:00:00:00:00:01"}]}

        f = ev.add_feature("ElectricalConnection", Role.SERVER)
        for fn in ("electricalConnectionDescriptionListData", "electricalConnectionParameterDescriptionListData",
                   "electricalConnectionPermittedValueSetListData"):
            f.add_function(fn)
        f.data["electricalConnectionDescriptionListData"] = {"electricalConnectionDescriptionData": [
            {"electricalConnectionId": 0, "powerSupplyType": "ac", "acConnectedPhases": self.phases,
             "positiveEnergyDirection": "consume"}]}
        params, permitted = [], []
        for i, phase in enumerate(phases):
            params.append({"electricalConnectionId": 0, "parameterId": i + 1, "measurementId": i + 1,
                           "voltageType": "ac", "acMeasuredPhases": phase, "acMeasuredInReferenceTo": "neutral",
                           "acMeasurementType": "real", "acMeasurementVariant": "rms"})
            permitted.append({"electricalConnectionId": 0, "parameterId": i + 1, "permittedValueSet": [
                {"value": [_sn(0)], "range": [{"min": _sn(self.min_current), "max": _sn(self.max_current)}]}]})
            params.append({"electricalConnectionId": 0, "parameterId": i + 4, "measurementId": i + 4,
                           "voltageType": "ac", "acMeasuredPhases": phase, "acMeasuredInReferenceTo": "neutral",
                           "acMeasurementType": "real", "acMeasurementVariant": "rms"})
        params.append({"electricalConnectionId": 0, "parameterId": 7, "scopeType": "acPowerTotal"})
        permitted.append({"electricalConnectionId": 0, "parameterId": 7, "permittedValueSet": [
            {"value": [_sn(0)], "range": [{"min": _sn(self.min_current * 230 * self.phases),
                                           "max": _sn(self.max_current * 230 * self.phases)}]}]})
        f.data["electricalConnectionParameterDescriptionListData"] = {
            "electricalConnectionParameterDescriptionData": params}
        f.data["electricalConnectionPermittedValueSetListData"] = {
            "electricalConnectionPermittedValueSetData": permitted}

        f = ev.add_feature("Measurement", Role.SERVER)
        for fn in ("measurementDescriptionListData", "measurementConstraintsListData", "measurementListData"):
            f.add_function(fn)
        descs = []
        for i, _phase in enumerate(phases):
            descs.append({"measurementId": i + 1, "measurementType": "current", "commodityType": "electricity",
                          "unit": "A", "scopeType": "acCurrent"})
            descs.append({"measurementId": i + 4, "measurementType": "power", "commodityType": "electricity",
                          "unit": "W", "scopeType": "acPower"})
        descs.append({"measurementId": 7, "measurementType": "energy", "commodityType": "electricity",
                      "unit": "Wh", "scopeType": "charge"})
        f.data["measurementDescriptionListData"] = {"measurementDescriptionData": descs}
        f.data["measurementConstraintsListData"] = {}
        f.data["measurementListData"] = {"measurementData": self._measurements(0.0, 0.0)}

        f = ev.add_feature("LoadControl", Role.SERVER)
        f.add_function("loadControlLimitDescriptionListData")
        f.add_function("loadControlLimitConstraintsListData")
        f.add_function("loadControlLimitListData", read=True, write=True)
        limit_descs, limits = [], []
        for i, _phase in enumerate(phases):
            limit_descs.append({"limitId": i + 1, "limitType": "maxValueLimit", "limitCategory": "obligation",
                                "measurementId": i + 1, "unit": "A", "scopeType": "overloadProtection"})
            limit_descs.append({"limitId": i + 4, "limitType": "maxValueLimit", "limitCategory": "recommendation",
                                "measurementId": i + 1, "unit": "A", "scopeType": "selfConsumption"})
            limits.append({"limitId": i + 1, "isLimitChangeable": True, "isLimitActive": False,
                           "value": _sn(self.max_current)})
            limits.append({"limitId": i + 4, "isLimitChangeable": True, "isLimitActive": False,
                           "value": _sn(self.max_current)})
        f.data["loadControlLimitDescriptionListData"] = {"loadControlLimitDescriptionData": limit_descs}
        f.data["loadControlLimitConstraintsListData"] = {}
        f.data["loadControlLimitListData"] = {"loadControlLimitData": sorted(limits, key=lambda x: x["limitId"])}
        f.write_approval = self._approve_limits

        ev.add_use_case("EV", "evCommissioningAndConfiguration", "1.0.1", [1, 2, 3, 4, 5, 6, 7, 8])
        ev.add_use_case("EV", "measurementOfElectricityDuringEvCharging", "1.0.1", [1, 2, 3])
        ev.add_use_case("EV", "overloadProtectionByEvChargingCurrentCurtailment", "1.0.1", [1, 2, 3])
        ev.add_use_case("EV", "optimizationOfSelfConsumptionDuringEvCharging", "1.0.1", [1, 2, 3])
        self.device.announce_entity(ev)
        return ev

    def unplug(self) -> None:
        if self.ev is not None:
            self.device.remove_entity(self.ev)
            self.ev = None

    def _measurements(self, current: float, energy: float) -> list[dict[str, Any]]:
        out = []
        for i in range(self.phases):
            out.append({"measurementId": i + 1, "valueType": "value", "value": _sn(current)})
            out.append({"measurementId": i + 4, "valueType": "value", "value": _sn(round(current * 230, 1))})
        out.append({"measurementId": 7, "valueType": "value", "value": _sn(energy)})
        return sorted(out, key=lambda x: x["measurementId"])

    def set_charging(self, current: float, energy_wh: float) -> None:
        """Update the measurements (notifies subscribers)."""
        if self.ev is None:
            return
        f = self.ev.feature("Measurement", Role.SERVER)
        f.set_data("measurementListData", {"measurementData": self._measurements(current, energy_wh)})

    @staticmethod
    def _approve_limits(msg: Message) -> None:
        return None  # accept every limit write

    def _on_event(self, event) -> None:
        if (event.type == "data" and event.classifier == "write" and event.function == "loadControlLimitListData"
                and self.ev is not None):
            data = self.ev.feature("LoadControl", Role.SERVER).data["loadControlLimitListData"]
            for item in data.get("loadControlLimitData", []):
                category = "obligation" if item["limitId"] <= 3 else "recommendation"
                phase = (item["limitId"] - 1) % 3
                if phase < self.phases:
                    value = scaled_value(item.get("value"))
                    if value is not None:
                        self.limits[category][phase] = value
            _LOGGER.info("limits written: %s", self.limits)

    async def start(self) -> None:
        await self.service.start()

    async def stop(self) -> None:
        await self.service.stop()
