"""Use case base and client feature helper (port of eebus-go's usecase and features/client)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..spine.device import (
    Change,
    Event,
    EventType,
    LocalEntity,
    LocalFeature,
    RemoteEntity,
    RemoteFeature,
    spawn,
)
from ..spine.model import Role, SpineError, as_list, filter_items, matches, scaled_value

_LOGGER = logging.getLogger(__name__)

# feature types
DEVICE_CLASSIFICATION = "DeviceClassification"
DEVICE_CONFIGURATION = "DeviceConfiguration"
DEVICE_DIAGNOSIS = "DeviceDiagnosis"
ELECTRICAL_CONNECTION = "ElectricalConnection"
IDENTIFICATION = "Identification"
LOAD_CONTROL = "LoadControl"
MEASUREMENT = "Measurement"

PHASES = ("a", "b", "c")

UseCaseEventCallback = Callable[[str, RemoteEntity | None, str], None]
"""Called with (ski, remote entity, event name)."""


class DataNotAvailable(LookupError):
    """The remote has not (yet) provided the requested data."""


@dataclass
class Scenario:
    id: int
    mandatory: bool = False
    server_features: tuple[str, ...] = ()


@dataclass
class RemoteEntityScenarios:
    entity: RemoteEntity
    scenarios: list[int] = field(default_factory=list)


# --- client feature ------------------------------------------------------------------


class ClientFeature:
    """A local client feature talking to the matching server feature of a remote entity."""

    def __init__(self, feature_type: str, local_entity: LocalEntity, remote_entity: RemoteEntity) -> None:
        local = local_entity.feature(feature_type, Role.CLIENT)
        if local is None:
            raise DataNotAvailable(f"local {feature_type} client not found")
        remote = remote_entity.feature(feature_type, Role.SERVER)
        if remote is None:
            raise DataNotAvailable(f"remote {feature_type} server not found")
        self.type = feature_type
        self.local: LocalFeature = local
        self.remote: RemoteFeature = remote

    @classmethod
    def find(cls, feature_type: str, local_entity: LocalEntity,
             remote_entity: RemoteEntity | None) -> ClientFeature | None:
        if remote_entity is None:
            return None
        try:
            return cls(feature_type, local_entity, remote_entity)
        except DataNotAvailable:
            return None

    def supports(self, function: str) -> bool:
        ops = self.remote.operations.get(function)
        return ops is not None and ops.read

    def request(self, *functions: str) -> None:
        """Read functions (fire and forget; the replies arrive as data events)."""
        for function in functions:
            if self.supports(function):
                self.local.read(self.remote, function)

    def subscribe(self) -> None:
        if not self.local.has_subscription(self.remote):
            spawn(_quietly(self.local.subscribe(self.remote), f"subscribe {self.remote}"))

    def bind(self) -> None:
        if not self.local.has_binding(self.remote):
            spawn(_quietly(self.local.bind(self.remote), f"bind {self.remote}"))

    def data(self, function: str) -> Any:
        return self.remote.get(function)

    def items(self, function: str, list_field: str) -> list[dict[str, Any]]:
        return as_list((self.remote.data.get(function) or {}).get(list_field))


async def _quietly(coro, what: str) -> None:
    try:
        await coro
    except (SpineError, TimeoutError) as err:
        _LOGGER.debug("%s failed: %s", what, err)


# --- data helpers (features/internal) --------------------------------------------------


def measurements(local: LocalEntity, entity: RemoteEntity, **description: Any) -> list[dict[str, Any]]:
    """Measurement data items whose description matches ``description``."""
    feature = ClientFeature.find(MEASUREMENT, local, entity)
    if feature is None:
        raise DataNotAvailable("no measurement feature")
    descs = filter_items(feature.items("measurementDescriptionListData", "measurementDescriptionData"),
                         description)
    ids = {d.get("measurementId") for d in descs}
    data = [m for m in feature.items("measurementListData", "measurementData")
            if m.get("measurementId") in ids]
    if not data:
        raise DataNotAvailable("no matching measurements")
    return data


def parameter_descriptions(local: LocalEntity, entity: RemoteEntity, **fields: Any) -> list[dict[str, Any]]:
    feature = ClientFeature.find(ELECTRICAL_CONNECTION, local, entity)
    if feature is None:
        raise DataNotAvailable("no electrical connection feature")
    return filter_items(feature.items("electricalConnectionParameterDescriptionListData",
                                      "electricalConnectionParameterDescriptionData"), fields)


def permitted_values(local: LocalEntity, entity: RemoteEntity,
                     parameter_id: int) -> tuple[float | None, float | None, float | None]:
    """(min, max, default) of a permitted value set."""
    feature = ClientFeature.find(ELECTRICAL_CONNECTION, local, entity)
    if feature is None:
        raise DataNotAvailable("no electrical connection feature")
    sets = filter_items(feature.items("electricalConnectionPermittedValueSetListData",
                                      "electricalConnectionPermittedValueSetData"),
                        parameterId=parameter_id)
    if len(sets) != 1:
        raise DataNotAvailable("no permitted values")
    low = high = default = None
    for value_set in as_list(sets[0].get("permittedValueSet")):
        values = as_list(value_set.get("value"))
        if values:
            default = scaled_value(values[0])
        for rng in as_list(value_set.get("range")):
            if rng.get("min") is not None:
                low = scaled_value(rng["min"])
            if rng.get("max") is not None:
                high = scaled_value(rng["max"])
    return low, high, default


def adjust_to_permitted(local: LocalEntity, entity: RemoteEntity, value: float, parameter_id: int) -> float:
    """Clamp ``value`` like eebus-go: below min -> default, above max -> max."""
    try:
        low, high, default = permitted_values(local, entity, parameter_id)
    except DataNotAvailable:
        return value
    if low is not None and default is not None and value < low:
        value = default
    if high is not None and value > high:
        value = high
    return value


def phase_parameters(local: LocalEntity, entity: RemoteEntity) -> dict[str, list[dict[str, Any]]]:
    return {phase: parameter_descriptions(local, entity, acMeasuredPhases=phase) for phase in PHASES}


def manufacturer_data(local: LocalEntity, entity: RemoteEntity) -> dict[str, Any]:
    feature = ClientFeature.find(DEVICE_CLASSIFICATION, local, entity)
    data = feature.data("deviceClassificationManufacturerData") if feature else None
    if not data:
        raise DataNotAvailable("no manufacturer data")
    return data


def diagnosis_state(local: LocalEntity, entity: RemoteEntity) -> dict[str, Any]:
    feature = ClientFeature.find(DEVICE_DIAGNOSIS, local, entity)
    data = feature.data("deviceDiagnosisStateData") if feature else None
    if not data:
        raise DataNotAvailable("no diagnosis state")
    return data


def configuration_value(local: LocalEntity, entity: RemoteEntity, key_name: str,
                        value_type: str) -> Any:
    feature = ClientFeature.find(DEVICE_CONFIGURATION, local, entity)
    if feature is None:
        raise DataNotAvailable("no device configuration feature")
    descs = filter_items(feature.items("deviceConfigurationKeyValueDescriptionListData",
                                       "deviceConfigurationKeyValueDescriptionData"),
                         keyName=key_name, valueType=value_type)
    ids = {d.get("keyId") for d in descs}
    for item in feature.items("deviceConfigurationKeyValueListData", "deviceConfigurationKeyValueData"):
        if item.get("keyId") in ids and isinstance(item.get("value"), dict):
            value = item["value"]
            if value_type == "scaledNumber":
                return scaled_value(value.get("scaledNumber"))
            if value_type in value:
                return value[value_type]
    raise DataNotAvailable(f"no configuration value {key_name}")


# --- use case base -------------------------------------------------------------------


class UseCase:
    """Common use case logic: announce support, track which remote entities support it."""

    actor = "CEM"
    name = ""
    version = "1.0.0"
    sub_revision = "release"
    scenarios: tuple[Scenario, ...] = ()
    valid_actors: tuple[str, ...] = ()
    valid_entity_types: tuple[str, ...] = ()
    client_features: tuple[str, ...] = ()

    # event names (eebus-go use case events)
    USE_CASE_SUPPORT_UPDATE = "UseCaseSupportUpdate"

    def __init__(self, local_entity: LocalEntity, on_event: UseCaseEventCallback | None = None) -> None:
        self.local = local_entity
        self.on_event = on_event
        self.remote_scenarios: list[RemoteEntityScenarios] = []
        local_entity.device.subscribe_events(self.handle_event)

    # setup
    def add_features(self) -> None:
        for feature_type in self.client_features:
            self.local.add_feature(feature_type, Role.CLIENT)

    def add_use_case(self) -> None:
        self.local.add_use_case(self.actor, self.name, self.version,
                                [s.id for s in self.scenarios], self.sub_revision)

    def setup(self) -> UseCase:
        self.add_features()
        self.add_use_case()
        return self

    def set_available(self, available: bool) -> None:
        self.local.set_use_case_available(self.actor, self.name, available)

    # remote entities
    def is_compatible(self, entity: RemoteEntity | None) -> bool:
        return entity is not None and entity.type in self.valid_entity_types

    def scenarios_for(self, entity: RemoteEntity) -> list[int]:
        for item in self.remote_scenarios:
            if item.entity is entity:
                return item.scenarios
        return []

    def is_scenario_available(self, entity: RemoteEntity, scenario: int) -> bool:
        return scenario in self.scenarios_for(entity)

    def _emit(self, ski: str, entity: RemoteEntity | None, event: str) -> None:
        if self.on_event:
            try:
                self.on_event(ski, entity, event)
            except Exception:
                _LOGGER.exception("use case event handler failed")

    def handle_event(self, event: Event) -> None:
        if event.type == EventType.DEVICE and event.change == Change.REMOVE and event.device:
            before = len(self.remote_scenarios)
            self.remote_scenarios = [s for s in self.remote_scenarios if s.entity.device is not event.device]
            if len(self.remote_scenarios) != before:
                self._emit(event.ski, None, self.USE_CASE_SUPPORT_UPDATE)
            return
        if event.type == EventType.ENTITY and event.change == Change.REMOVE and event.entity:
            before = len(self.remote_scenarios)
            self.remote_scenarios = [s for s in self.remote_scenarios if s.entity is not event.entity]
            if len(self.remote_scenarios) != before:
                self._emit(event.ski, event.entity, self.USE_CASE_SUPPORT_UPDATE)
        use_case_relevant = (
            (event.type == EventType.DATA and event.function == "nodeManagementUseCaseData")
            or (event.type in (EventType.DEVICE, EventType.ENTITY) and event.change == Change.ADD))
        if use_case_relevant and event.device is not None:
            self._update_use_case_support(event)
        if self.is_compatible(event.entity):
            self.handle_entity_event(event)

    def handle_entity_event(self, event: Event) -> None:
        """Use case specific handling of events for compatible remote entities."""

    def _update_use_case_support(self, event: Event) -> None:
        device = event.device
        for info in device.use_cases():
            if info.get("actor") not in self.valid_actors:
                continue
            for support in as_list(info.get("useCaseSupport")):
                if support.get("useCaseName") != self.name or support.get("scenarioSupport") is None:
                    continue
                supported = set(as_list(support.get("scenarioSupport")))
                entities = []
                address = info.get("address") or {}
                if address.get("entity"):
                    entity_addr = list(address["entity"])
                    if info.get("actor") == "EV" and len(entity_addr) == 1:
                        entity_addr.append(1)
                    if (found := device.entity(entity_addr)) is not None:
                        entities.append(found)
                if not entities:
                    entities = list(device.entities)
                for entity in entities:
                    if entity.type not in self.valid_entity_types:
                        continue
                    available = []
                    for scenario in self.scenarios:
                        if scenario.id not in supported:
                            continue
                        found_types = {f.type for f in entity.features if f.role == Role.SERVER}
                        if set(scenario.server_features) <= found_types:
                            available.append(scenario.id)
                    self._set_scenarios(device.ski, entity, available)

    def _set_scenarios(self, ski: str, entity: RemoteEntity, scenarios: list[int]) -> None:
        for item in self.remote_scenarios:
            if item.entity is entity:
                if item.scenarios != scenarios:
                    item.scenarios = scenarios
                    self._emit(ski, entity, self.USE_CASE_SUPPORT_UPDATE)
                return
        self.remote_scenarios.append(RemoteEntityScenarios(entity, scenarios))
        self._emit(ski, entity, self.USE_CASE_SUPPORT_UPDATE)

    @staticmethod
    def is_entity_added(event: Event) -> bool:
        return event.type == EventType.ENTITY and event.change == Change.ADD

    @staticmethod
    def is_entity_removed(event: Event) -> bool:
        return event.type == EventType.ENTITY and event.change == Change.REMOVE

    @staticmethod
    def is_data_update(event: Event, function: str) -> bool:
        return event.type == EventType.DATA and event.function == function and event.local_feature is not None


def items_match(items: list[dict[str, Any]], selector: dict[str, Any]) -> bool:
    return any(matches(i, selector) for i in items)
