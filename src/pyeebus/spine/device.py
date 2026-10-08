"""SPINE devices, entities and features (port of spine-go's spine package).

The local device holds entities and features with their function data,
answers reads, keeps subscriptions and bindings and sends heartbeats. Remote
devices mirror what a peer announced via detailed discovery, including the
data received from it.

Everything runs on one asyncio loop. Incoming messages are handled
synchronously; outgoing datagrams go through a per-peer ``send`` callable.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import itertools
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import schema
from .model import (
    DEFAULT_MAX_RESPONSE_DELAY,
    SPECIFICATION_VERSION,
    Address,
    CmdClassifier,
    ErrorNumber,
    Role,
    SpineError,
    as_list,
    format_datetime,
    format_duration,
    parse_duration,
)
from .update import filter_parts, update_data

_LOGGER = logging.getLogger(__name__)

NODE_MANAGEMENT = "NodeManagement"
DEVICE_INFORMATION = "DeviceInformation"
DEVICE_CLASSIFICATION = "DeviceClassification"
DEVICE_DIAGNOSIS = "DeviceDiagnosis"

FN_DETAILED_DISCOVERY = "nodeManagementDetailedDiscoveryData"
FN_USE_CASE = "nodeManagementUseCaseData"
FN_SUBSCRIPTION = "nodeManagementSubscriptionData"
FN_SUBSCRIPTION_REQUEST = "nodeManagementSubscriptionRequestCall"
FN_SUBSCRIPTION_DELETE = "nodeManagementSubscriptionDeleteCall"
FN_BINDING = "nodeManagementBindingData"
FN_BINDING_REQUEST = "nodeManagementBindingRequestCall"
FN_BINDING_DELETE = "nodeManagementBindingDeleteCall"
FN_DESTINATION_LIST = "nodeManagementDestinationListData"
FN_HEARTBEAT = "deviceDiagnosisHeartbeatData"
FN_MANUFACTURER = "deviceClassificationManufacturerData"
FN_RESULT = "resultData"


_background: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    """Run a coroutine in the background (keeps a reference until it is done)."""
    task = asyncio.get_running_loop().create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


# --- events ------------------------------------------------------------------------


class EventType:
    DEVICE = "device"  # remote device added (after discovery) or removed
    ENTITY = "entity"  # remote entity added or removed
    DATA = "data"  # remote data changed (reply/notify) or local data written by remote
    SUBSCRIPTION = "subscription"
    BINDING = "binding"


class Change:
    ADD = "add"
    REMOVE = "remove"
    UPDATE = "update"


@dataclass
class Event:
    type: str
    change: str
    ski: str
    device: RemoteDevice | None = None
    entity: RemoteEntity | None = None
    feature: RemoteFeature | None = None
    local_feature: LocalFeature | None = None
    function: str | None = None
    classifier: str | None = None
    data: Any = None


EventCallback = Callable[[Event], None]


# --- messages ------------------------------------------------------------------------


@dataclass
class Message:
    header: dict[str, Any]
    cmd: dict[str, Any]
    classifier: str
    function: str | None
    data: Any
    filters: list[dict[str, Any]]
    device: RemoteDevice
    entity: RemoteEntity | None
    feature: RemoteFeature


def cmd_function(cmd: dict[str, Any]) -> tuple[str | None, Any]:
    """The function name and data carried by a SPINE cmd."""
    for key, value in cmd.items():
        if key not in ("function", "filter", "manufacturerSpecificExtension", "lastUpdateAt"):
            return key, value
    return cmd.get("function"), None


def make_cmd(function: str, data: Any, filters: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    cmd: dict[str, Any] = {}
    if filters:
        cmd["function"] = function
        cmd["filter"] = filters
    cmd[function] = data if data is not None else {}
    return cmd


def partial_filter(selector: dict[str, Any] | None = None, function: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"cmdControl": {"partial": {}}}
    if selector and function and function in schema.SELECTORS:
        out[schema.SELECTORS[function]] = selector
    return out


def delete_filter(selector: dict[str, Any] | None = None, function: str | None = None,
                  elements: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"cmdControl": {"delete": {}}}
    if selector and function and function in schema.SELECTORS:
        out[schema.SELECTORS[function]] = selector
    if elements and function and function in schema.ELEMENTS:
        out[schema.ELEMENTS[function]] = elements
    return out


# --- operations ----------------------------------------------------------------------


@dataclass
class Operations:
    read: bool = False
    read_partial: bool = False
    write: bool = False
    write_partial: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.read:
            out["read"] = {"partial": {}} if self.read_partial else {}
        if self.write:
            out["write"] = {"partial": {}} if self.write_partial else {}
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Operations:
        data = data or {}
        read, write = data.get("read"), data.get("write")
        return cls(read is not None, isinstance(read, dict) and "partial" in read,
                   write is not None, isinstance(write, dict) and "partial" in write)


# --- sender --------------------------------------------------------------------------


class Sender:
    """Builds SPINE datagrams for one remote device and hands them to ``send``."""

    def __init__(self, send: Callable[[dict[str, Any]], None]) -> None:
        self._send = send
        self._counter = itertools.count(1)
        self._pending_requests: dict[str, int] = {}  # hash -> msgCounter of unanswered requests

    def _datagram(self, classifier: str, source: dict, destination: dict, cmds: list[dict],
                  *, reference: int | None = None, ack: bool = False) -> int:
        counter = next(self._counter)
        header: dict[str, Any] = {
            "specificationVersion": SPECIFICATION_VERSION,
            "addressSource": source,
            "addressDestination": destination,
            "msgCounter": counter,
        }
        if reference is not None:
            header["msgCounterReference"] = reference
        header["cmdClassifier"] = classifier
        if ack:
            header["ackRequest"] = True
        self._send({"datagram": {"header": header, "payload": {"cmd": cmds}}})
        return counter

    def response_received(self, reference: int) -> None:
        for key, counter in list(self._pending_requests.items()):
            if counter == reference:
                del self._pending_requests[key]

    def request(self, classifier: str, source: Address, destination: Address, cmds: list[dict],
                ack: bool = False) -> int:
        """Send a read/call; an identical unanswered request is not sent twice."""
        key = hashlib.sha256(f"{destination}-{json.dumps(cmds, sort_keys=True)}".encode()).hexdigest()
        if key in self._pending_requests:
            return self._pending_requests[key]
        counter = self._datagram(classifier, source.to_dict(), destination.to_dict(), cmds, ack=ack)
        self._pending_requests[key] = counter
        if len(self._pending_requests) > 20:
            del self._pending_requests[next(iter(self._pending_requests))]
        return counter

    def _response(self, classifier: str, request: dict, local_device: str | None, cmd: dict) -> int:
        source = dict(request.get("addressDestination") or {})
        if local_device is not None:
            source = {"device": local_device, **{k: v for k, v in source.items() if k != "device"}}
        return self._datagram(classifier, source, request.get("addressSource") or {}, [cmd],
                              reference=request.get("msgCounter"))

    def reply(self, request: dict, local_device: str | None, cmd: dict) -> int:
        return self._response(CmdClassifier.REPLY, request, local_device, cmd)

    def result(self, request: dict, local_device: str | None, error: SpineError | None = None) -> int:
        data: dict[str, Any] = {"errorNumber": error.number if error else ErrorNumber.NO_ERROR}
        if error and error.description:
            data["description"] = error.description
        return self._response(CmdClassifier.RESULT, request, local_device, {FN_RESULT: data})

    def notify(self, source: Address, destination: Address, cmd: dict) -> int:
        return self._datagram(CmdClassifier.NOTIFY, source.to_dict(), destination.to_dict(), [cmd])

    def write(self, source: Address, destination: Address, cmd: dict) -> int:
        return self._datagram(CmdClassifier.WRITE, source.to_dict(), destination.to_dict(), [cmd],
                              ack=True)

    def _node_management_call(self, client: Address, server: Address, function: str,
                              body: dict) -> int:
        local = Address(client.device, (0,), 0)
        remote = Address(server.device, (0,), 0)
        return self.request(CmdClassifier.CALL, local, remote, [{function: body}], ack=True)

    def subscribe(self, client: Address, server: Address, server_type: str) -> int:
        return self._node_management_call(client, server, FN_SUBSCRIPTION_REQUEST, {
            "subscriptionRequest": {"clientAddress": client.to_dict(), "serverAddress": server.to_dict(),
                                    "serverFeatureType": server_type}})

    def unsubscribe(self, client: Address, server: Address) -> int:
        return self._node_management_call(client, server, FN_SUBSCRIPTION_DELETE, {
            "subscriptionDelete": {"clientAddress": client.to_dict(), "serverAddress": server.to_dict()}})

    def bind(self, client: Address, server: Address, server_type: str) -> int:
        return self._node_management_call(client, server, FN_BINDING_REQUEST, {
            "bindingRequest": {"clientAddress": client.to_dict(), "serverAddress": server.to_dict(),
                               "serverFeatureType": server_type}})

    def unbind(self, client: Address, server: Address) -> int:
        return self._node_management_call(client, server, FN_BINDING_DELETE, {
            "bindingDelete": {"clientAddress": client.to_dict(), "serverAddress": server.to_dict()}})


# --- remote side -----------------------------------------------------------------------


class RemoteFeature:
    def __init__(self, entity: RemoteEntity, feature_id: int, feature_type: str, role: str) -> None:
        self.entity = entity
        self.id = feature_id
        self.type = feature_type
        self.role = role
        self.description: str | None = None
        self.specific_usage: list[str] = []
        self.operations: dict[str, Operations] = {}
        self.max_response_delay = DEFAULT_MAX_RESPONSE_DELAY
        self.data: dict[str, Any] = {}

    @property
    def device(self) -> RemoteDevice:
        return self.entity.device

    @property
    def address(self) -> Address:
        return Address(self.device.address, self.entity.address.entity, self.id)

    def get(self, function: str) -> Any:
        return copy.deepcopy(self.data.get(function))

    def update(self, function: str, data: Any, filters: list[dict] | None = None) -> None:
        self.data[function] = update_data(function, self.data.get(function), data, filters)

    def __repr__(self) -> str:
        return f"<RemoteFeature {self.type}/{self.role} {self.address}>"


class RemoteEntity:
    def __init__(self, device: RemoteDevice, address: tuple[int, ...], entity_type: str) -> None:
        self.device = device
        self.entity = address
        self.type = entity_type
        self.description: str | None = None
        self.features: list[RemoteFeature] = []

    @property
    def address(self) -> Address:
        return Address(self.device.address, self.entity)

    def feature(self, feature_type: str, role: str) -> RemoteFeature | None:
        return next((f for f in self.features if f.type == feature_type and f.role == role), None)

    def feature_by_id(self, feature_id: int | None) -> RemoteFeature | None:
        return next((f for f in self.features if f.id == feature_id), None)

    def __repr__(self) -> str:
        return f"<RemoteEntity {self.type} {self.address}>"


class RemoteDevice:
    def __init__(self, local: LocalDevice, ski: str, sender: Sender) -> None:
        self.local = local
        self.ski = ski
        self.sender = sender
        self.address: str | None = None
        self.device_type: str | None = None
        self.feature_set: str | None = None
        self.entities: list[RemoteEntity] = []
        info = RemoteEntity(self, (0,), DEVICE_INFORMATION)
        info.features.append(RemoteFeature(info, 0, NODE_MANAGEMENT, Role.SPECIAL))
        self.entities.append(info)

    def entity(self, address: tuple[int, ...] | list[int]) -> RemoteEntity | None:
        address = tuple(address)
        return next((e for e in self.entities if e.entity == address), None)

    def feature_by_address(self, address: Address) -> RemoteFeature | None:
        entity = self.entity(address.entity)
        return entity.feature_by_id(address.feature) if entity else None

    @property
    def node_management(self) -> RemoteFeature:
        return self.entities[0].features[0]

    def use_cases(self) -> list[dict[str, Any]]:
        data = self.node_management.data.get(FN_USE_CASE) or {}
        return as_list(data.get("useCaseInformation"))

    def handle_payload(self, payload: dict[str, Any]) -> None:
        """Process one SPINE datagram (``{"datagram": {...}}``) from this device."""
        datagram = payload.get("datagram") or {}
        header = datagram.get("header") or {}
        if header.get("msgCounterReference") is not None:
            self.sender.response_received(header["msgCounterReference"])
        try:
            self.local.process_datagram(datagram, self)
        except Exception:
            _LOGGER.exception("error handling SPINE message from %s", self.ski[:8])

    # discovery data -> entities and features
    def update_from_discovery(self, data: dict[str, Any], only: tuple[int, ...] | None = None,
                              initial: bool = False) -> list[RemoteEntity]:
        description = ((data.get("deviceInformation") or {}).get("description")) or {}
        if initial:
            addr = (description.get("deviceAddress") or {}).get("device")
            if addr:
                self.address = addr
            self.device_type = description.get("deviceType", self.device_type)
            self.feature_set = description.get("networkFeatureSet", self.feature_set)
        added = []
        for info in as_list(data.get("entityInformation")):
            desc = info.get("description") or {}
            address = tuple(as_list((desc.get("entityAddress") or {}).get("entity")))
            if not address or (only is not None and address != only):
                continue
            entity = self.entity(address)
            if entity is None:
                entity = RemoteEntity(self, address, desc.get("entityType", ""))
                self.entities.append(entity)
                added.append(entity)
            entity.description = desc.get("description")
            old = {(f.type, f.role, f.id): f for f in entity.features}
            entity.features = []
            for finfo in as_list(data.get("featureInformation")):
                fdesc = finfo.get("description") or {}
                faddr = fdesc.get("featureAddress") or {}
                if tuple(as_list(faddr.get("entity"))) != address or faddr.get("feature") is None:
                    continue
                key = (fdesc.get("featureType", ""), fdesc.get("role", ""), faddr["feature"])
                feature = old.get(key) or RemoteFeature(entity, faddr["feature"], key[0], key[1])
                feature.description = fdesc.get("description")
                feature.specific_usage = as_list(fdesc.get("specificUsage"))
                feature.operations = {
                    sf["function"]: Operations.from_dict(sf.get("possibleOperations"))
                    for sf in as_list(fdesc.get("supportedFunction")) if sf.get("function")}
                if fdesc.get("maxResponseDelay"):
                    with contextlib.suppress(ValueError):
                        feature.max_response_delay = parse_duration(fdesc["maxResponseDelay"])
                entity.features.append(feature)
        return added

    def remove_entity(self, address: tuple[int, ...]) -> RemoteEntity | None:
        entity = self.entity(address)
        if entity is not None:
            self.entities.remove(entity)
        return entity

    def __repr__(self) -> str:
        return f"<RemoteDevice {self.address or self.ski[:8]}>"


# --- local side -------------------------------------------------------------------------


WriteApproval = Callable[["Message"], SpineError | None]


class LocalFeature:
    def __init__(self, entity: LocalEntity, feature_id: int, feature_type: str, role: str,
                 description: str | None = None) -> None:
        self.entity = entity
        self.id = feature_id
        self.type = feature_type
        self.role = role
        self.description = description
        self.operations: dict[str, Operations] = {}
        self.data: dict[str, Any] = {}
        self._waiters: dict[int, list[asyncio.Future]] = {}
        self.result_callbacks: list[Callable[[Message], None]] = []
        self.write_approval: WriteApproval | None = None

    @property
    def device(self) -> LocalDevice:
        return self.entity.device

    @property
    def address(self) -> Address:
        return Address(self.device.address, self.entity.entity, self.id)

    def add_function(self, function: str, read: bool = True, write: bool = False) -> None:
        if self.role not in (Role.SERVER, Role.SPECIAL) or function in self.operations:
            return
        self.operations[function] = Operations(
            read=read, write=write, write_partial=write and function in schema.LIST_FUNCTIONS)
        if self.role == Role.SERVER and self.type == DEVICE_DIAGNOSIS and function == FN_HEARTBEAT:
            self.entity.start_heartbeat(self)

    def information(self) -> dict[str, Any]:
        desc: dict[str, Any] = {
            "featureAddress": {"entity": list(self.entity.entity), "feature": self.id},
            "featureType": self.type,
            "role": self.role,
        }
        if self.operations:
            desc["supportedFunction"] = [
                {"function": fn, "possibleOperations": ops.to_dict()}
                for fn, ops in self.operations.items()]
        if self.description:
            desc["description"] = self.description
        return {"description": desc}

    # local data
    def get(self, function: str) -> Any:
        return copy.deepcopy(self.data.get(function))

    def set_data(self, function: str, data: Any, notify: bool = True) -> None:
        self.data[function] = copy.deepcopy(data)
        if notify and function not in (FN_BINDING, FN_SUBSCRIPTION):
            self.device.notify_subscribers(self.address, make_cmd(function, self.data[function]))

    # responses to our requests
    def _waiter(self, counter: int) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(counter, []).append(future)
        return future

    def _resolve(self, reference: int | None, value: Any = None,
                 error: Exception | None = None) -> None:
        for future in self._waiters.pop(reference, []) if reference is not None else []:
            if future.done():
                continue
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(value)

    async def _await(self, counter: int, timeout: float) -> Any:
        future = self._waiter(counter)
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            waiters = self._waiters.get(counter)
            if waiters and future in waiters:
                waiters.remove(future)
                if not waiters:
                    del self._waiters[counter]

    # requests to remote features
    def read(self, remote: RemoteFeature, function: str, selector: dict | None = None,
             elements: dict | None = None) -> int:
        filters = None
        ops = remote.operations.get(function)
        if (selector or elements) and ops and ops.read_partial:
            filt = partial_filter(selector, function)
            if elements and function in schema.ELEMENTS:
                filt[schema.ELEMENTS[function]] = elements
            filters = [filt]
        cmd = make_cmd(function, None, filters)
        return remote.device.sender.request(CmdClassifier.READ, self.address, remote.address, [cmd])

    async def request(self, remote: RemoteFeature, function: str, selector: dict | None = None,
                      timeout: float | None = None) -> Any:
        """Read ``function`` from ``remote`` and return the reply data."""
        ops = remote.operations.get(function)
        if ops is not None and not ops.read:
            raise SpineError(ErrorNumber.COMMAND_NOT_SUPPORTED, f"{function} is not readable")
        counter = self.read(remote, function, selector)
        return await self._await(counter, timeout or remote.max_response_delay)

    def write(self, remote: RemoteFeature, function: str, data: Any,
              filters: list[dict] | None = None) -> int:
        cmd = make_cmd(function, data, filters)
        counter = remote.device.sender.write(self.address, remote.address, cmd)
        return counter

    async def write_and_wait(self, remote: RemoteFeature, function: str, data: Any,
                             filters: list[dict] | None = None, timeout: float | None = None) -> None:
        """Write and wait for the result (raises :class:`SpineError` on rejection)."""
        counter = self.write(remote, function, data, filters)
        await self._await(counter, timeout or remote.max_response_delay)

    def has_subscription(self, remote: RemoteFeature) -> bool:
        return self.device.has_subscription(self.address, remote.address)

    async def subscribe(self, remote: RemoteFeature, timeout: float | None = None) -> None:
        if self.role == Role.SERVER or remote.role == Role.CLIENT:
            raise SpineError(ErrorNumber.COMMAND_REJECTED, "subscriptions go from client to server")
        if self.has_subscription(remote):
            return
        counter = remote.device.sender.subscribe(self.address, remote.address, remote.type)
        # the result comes back to our node management feature
        await self.device.node_management._await(
            counter, timeout or remote.device.node_management.max_response_delay)
        self.device.add_subscription(remote.device, self.address, remote.address)

    async def unsubscribe(self, remote: RemoteFeature, timeout: float | None = None) -> None:
        counter = remote.device.sender.unsubscribe(self.address, remote.address)
        await self.device.node_management._await(
            counter, timeout or remote.device.node_management.max_response_delay)
        self.device.remove_subscription(self.address, remote.address)

    def has_binding(self, remote: RemoteFeature) -> bool:
        return self.device.has_binding(self.address, remote.address)

    async def bind(self, remote: RemoteFeature, timeout: float | None = None) -> None:
        if self.role == Role.SERVER or remote.role == Role.CLIENT:
            raise SpineError(ErrorNumber.COMMAND_REJECTED, "bindings go from client to server")
        if self.has_binding(remote):
            return
        counter = remote.device.sender.bind(self.address, remote.address, remote.type)
        await self.device.node_management._await(
            counter, timeout or remote.device.node_management.max_response_delay)
        self.device.add_binding(remote.device, self.address, remote.address)

    # incoming messages
    def handle(self, msg: Message) -> None:
        if msg.classifier == CmdClassifier.RESULT:
            self._handle_result(msg)
        elif msg.classifier == CmdClassifier.READ:
            if self.role == Role.CLIENT:
                raise SpineError(ErrorNumber.COMMAND_REJECTED)
            if msg.function not in self.operations or not self.operations[msg.function].read:
                raise SpineError(ErrorNumber.COMMAND_NOT_SUPPORTED, "function not supported")
            msg.device.sender.reply(msg.header, self.device.address,
                                    make_cmd(msg.function, self.data.get(msg.function)))
        elif msg.classifier in (CmdClassifier.REPLY, CmdClassifier.NOTIFY):
            msg.feature.update(msg.function, msg.data, msg.filters)
            self.device.publish(Event(EventType.DATA, Change.UPDATE, msg.device.ski, msg.device,
                                      msg.entity, msg.feature, self, msg.function, msg.classifier,
                                      msg.feature.get(msg.function)))
            if msg.classifier == CmdClassifier.REPLY:
                self._resolve(msg.header.get("msgCounterReference"), msg.feature.get(msg.function))
        elif msg.classifier == CmdClassifier.WRITE:
            error = self.write_approval(msg) if self.write_approval else None
            if error is not None:
                raise error
            self.data[msg.function] = update_data(msg.function, self.data.get(msg.function),
                                                  msg.data, msg.filters)
            self.device.notify_subscribers(self.address, make_cmd(msg.function, self.data[msg.function]))
            self.device.publish(Event(EventType.DATA, Change.UPDATE, msg.device.ski, msg.device,
                                      msg.entity, msg.feature, self, msg.function, msg.classifier,
                                      self.get(msg.function)))
        else:
            raise SpineError(ErrorNumber.COMMAND_NOT_SUPPORTED, f"{msg.classifier} not supported")

    def _handle_result(self, msg: Message) -> None:
        data = msg.data or {}
        number = data.get("errorNumber")
        reference = msg.header.get("msgCounterReference")
        if number:
            _LOGGER.debug("result error %s from %s: %s", number, msg.feature, data.get("description"))
            self._resolve(reference, error=SpineError(number, data.get("description")))
        else:
            self._resolve(reference, None)
        for callback in self.result_callbacks:
            callback(msg)

    def __repr__(self) -> str:
        return f"<LocalFeature {self.type}/{self.role} {self.address}>"


class LocalEntity:
    def __init__(self, device: LocalDevice, address: tuple[int, ...], entity_type: str,
                 heartbeat_timeout: float = 4.0) -> None:
        self.device = device
        self.entity = address
        self.type = entity_type
        self.features: list[LocalFeature] = []
        self._next_id = 0 if address == (0,) else 1
        self.heartbeat_timeout = heartbeat_timeout
        self._heartbeat_feature: LocalFeature | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._heartbeat_counter = 0

    @property
    def address(self) -> Address:
        return Address(self.device.address, self.entity)

    def feature(self, feature_type: str, role: str) -> LocalFeature | None:
        return next((f for f in self.features if f.type == feature_type and f.role == role), None)

    def feature_by_id(self, feature_id: int | None) -> LocalFeature | None:
        return next((f for f in self.features if f.id == feature_id), None)

    def add_feature(self, feature_type: str, role: str, description: str | None = None,
                    cls: type[LocalFeature] = LocalFeature) -> LocalFeature:
        if (existing := self.feature(feature_type, role)) is not None:
            return existing
        if description is None:
            description = feature_type + {Role.CLIENT: " Client", Role.SERVER: " Server"}.get(role, "")
        feature = cls(self, self._next_id, feature_type, role, description)
        self._next_id += 1
        self.features.append(feature)
        return feature

    def information(self) -> dict[str, Any]:
        return {"description": {"entityAddress": {"entity": list(self.entity)}, "entityType": self.type}}

    # use cases
    def add_use_case(self, actor: str, name: str, version: str, scenarios: list[int],
                     sub_revision: str = "release", available: bool = True) -> None:
        nm = self.device.node_management
        data = nm.get(FN_USE_CASE) or {}
        infos = as_list(data.get("useCaseInformation"))
        address = {"device": self.device.address, "entity": list(self.entity)}
        support = {"useCaseName": name, "useCaseVersion": version, "useCaseAvailable": available,
                   "scenarioSupport": list(scenarios), "useCaseDocumentSubRevision": sub_revision}
        info = next((i for i in infos if i.get("address") == address and i.get("actor") == actor), None)
        if info is None:
            infos.append({"address": address, "actor": actor, "useCaseSupport": [support]})
        else:
            supports = [s for s in as_list(info.get("useCaseSupport")) if s.get("useCaseName") != name]
            info["useCaseSupport"] = [*supports, support]
        nm.set_data(FN_USE_CASE, {"useCaseInformation": infos})

    def set_use_case_available(self, actor: str, name: str, available: bool) -> None:
        nm = self.device.node_management
        data = nm.get(FN_USE_CASE) or {}
        for info in as_list(data.get("useCaseInformation")):
            if info.get("actor") == actor and tuple(info.get("address", {}).get("entity", ())) == self.entity:
                for support in as_list(info.get("useCaseSupport")):
                    if support.get("useCaseName") == name:
                        support["useCaseAvailable"] = available
        nm.set_data(FN_USE_CASE, data)

    # heartbeat (DeviceDiagnosis server)
    def start_heartbeat(self, feature: LocalFeature) -> None:
        self._heartbeat_feature = feature
        self._set_heartbeat(notify=False)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # started by LocalDevice.start()
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = loop.create_task(self._heartbeat_loop())

    def _set_heartbeat(self, notify: bool = True) -> None:
        if self._heartbeat_feature is None:
            return
        self._heartbeat_counter += 1
        self._heartbeat_feature.set_data(FN_HEARTBEAT, {
            "timestamp": format_datetime(),
            "heartbeatCounter": self._heartbeat_counter,
            "heartbeatTimeout": format_duration(self.heartbeat_timeout),
        }, notify=notify)

    async def _heartbeat_loop(self) -> None:
        # Like spine-go: send 2 s before the timeout, some EVSEs (Elli) treat the
        # timeout as the maximum interval between heartbeats.
        interval = self.heartbeat_timeout - 2 if self.heartbeat_timeout > 2 else self.heartbeat_timeout
        while True:
            self._set_heartbeat()
            await asyncio.sleep(interval)

    def stop_heartbeat(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

    def __repr__(self) -> str:
        return f"<LocalEntity {self.type} {self.address}>"


class LocalDevice:
    """The local SPINE device (one per process)."""

    def __init__(self, address: str, device_type: str, *, brand: str, model: str,
                 serial: str = "", ship_id: str = "", feature_set: str = "smart",
                 heartbeat_timeout: float = 4.0) -> None:
        self.address = address
        self.device_type = device_type
        self.feature_set = feature_set
        self.brand, self.model, self.serial, self.ship_id = brand, model, serial, ship_id
        self.heartbeat_timeout = heartbeat_timeout
        self.entities: list[LocalEntity] = []
        self.remote_devices: dict[str, RemoteDevice] = {}
        self.subscriptions: list[tuple[Address, Address]] = []  # (client, server)
        self.bindings: list[tuple[Address, Address]] = []
        self._listeners: list[EventCallback] = []
        self._subscription_id = itertools.count()
        self._binding_id = itertools.count()

        info = LocalEntity(self, (0,), DEVICE_INFORMATION)
        self.entities.append(info)
        self.node_management = info.add_feature(NODE_MANAGEMENT, Role.SPECIAL)
        nm = self.node_management
        for fn in (FN_DETAILED_DISCOVERY, FN_USE_CASE, FN_SUBSCRIPTION, FN_BINDING):
            nm.add_function(fn, read=True)
        for fn in (FN_SUBSCRIPTION_REQUEST, FN_SUBSCRIPTION_DELETE, FN_BINDING_REQUEST,
                   FN_BINDING_DELETE):
            nm.add_function(fn, read=False)
        if feature_set != "simple":
            nm.add_function(FN_DESTINATION_LIST, read=True)
        nm.data[FN_USE_CASE] = {}
        classification = info.add_feature(DEVICE_CLASSIFICATION, Role.SERVER)
        classification.add_function(FN_MANUFACTURER, read=True)
        classification.data[FN_MANUFACTURER] = {
            "deviceName": model, "deviceCode": ship_id, "serialNumber": serial,
            "vendorName": brand, "brandName": brand}

    # entities
    def add_entity(self, entity_type: str, address: tuple[int, ...] | None = None,
                   announce: bool = True) -> LocalEntity:
        """Add an entity (next free top level address by default).

        With ``announce=False`` add the features first and then call
        :meth:`announce_entity`, so subscribers learn about them in one go.
        """
        if address is None:
            address = (max((e.entity[0] for e in self.entities), default=-1) + 1,)
        entity = LocalEntity(self, tuple(address), entity_type, self.heartbeat_timeout)
        self.entities.append(entity)
        if announce:
            self._notify_entity(entity, "added")
        return entity

    def announce_entity(self, entity: LocalEntity) -> None:
        self._notify_entity(entity, "added")

    def remove_entity(self, entity: LocalEntity) -> None:
        entity.stop_heartbeat()
        if entity in self.entities:
            self.entities.remove(entity)
        nm = self.node_management
        data = nm.get(FN_USE_CASE) or {}
        infos = [i for i in as_list(data.get("useCaseInformation"))
                 if tuple(as_list((i.get("address") or {}).get("entity"))) != entity.entity]
        nm.data[FN_USE_CASE] = {"useCaseInformation": infos}
        self.subscriptions = [s for s in self.subscriptions
                              if not any(a.device == self.address and a.entity == entity.entity for a in s)]
        self.bindings = [b for b in self.bindings
                         if not any(a.device == self.address and a.entity == entity.entity for a in b)]
        self._notify_entity(entity, "removed")
        self.notify_subscribers(nm.address, make_cmd(FN_USE_CASE, nm.data[FN_USE_CASE]))

    def entity(self, address: tuple[int, ...] | list[int]) -> LocalEntity | None:
        address = tuple(address)
        return next((e for e in self.entities if e.entity == address), None)

    def feature_by_address(self, address: Address) -> LocalFeature | None:
        entity = self.entity(address.entity)
        return entity.feature_by_id(address.feature) if entity else None

    def information(self) -> dict[str, Any]:
        return {"description": {"deviceAddress": {"device": self.address},
                                "deviceType": self.device_type,
                                "networkFeatureSet": self.feature_set}}

    def detailed_discovery(self) -> dict[str, Any]:
        return {
            "specificationVersionList": {"specificationVersion": [SPECIFICATION_VERSION]},
            "deviceInformation": self.information(),
            "entityInformation": [e.information() for e in self.entities],
            "featureInformation": [f.information() for e in self.entities for f in e.features],
        }

    def _notify_entity(self, entity: LocalEntity, state: str) -> None:
        info = entity.information()
        info["description"]["lastStateChange"] = state
        data = {
            "specificationVersionList": {"specificationVersion": [SPECIFICATION_VERSION]},
            "deviceInformation": self.information(),
            "entityInformation": [info],
        }
        if state == "added":
            data["featureInformation"] = [f.information() for f in entity.features]
        self.notify_subscribers(self.node_management.address,
                                make_cmd(FN_DETAILED_DISCOVERY, data, [partial_filter()]))

    def start(self) -> None:
        """Start heartbeats (call from the running loop)."""
        for entity in self.entities:
            if entity._heartbeat_feature is not None:
                entity.start_heartbeat(entity._heartbeat_feature)

    def stop(self) -> None:
        for entity in self.entities:
            entity.stop_heartbeat()

    # events
    def subscribe_events(self, callback: EventCallback) -> None:
        self._listeners.append(callback)

    def unsubscribe_events(self, callback: EventCallback) -> None:
        with contextlib.suppress(ValueError):
            self._listeners.remove(callback)

    def publish(self, event: Event) -> None:
        self._handle_event(event)
        for callback in list(self._listeners):
            try:
                callback(event)
            except Exception:
                _LOGGER.exception("event handler failed")

    def _handle_event(self, event: Event) -> None:
        # after the detailed discovery: subscribe to the remote node management and
        # request its use cases (as spine-go's DeviceLocal.HandleEvent)
        if event.type == EventType.DEVICE and event.change == Change.ADD and event.device:
            remote = event.device
            nm = self.node_management
            spawn(self._subscribe_quietly(nm, remote.node_management))
            nm.read(remote.node_management, FN_USE_CASE)

    @staticmethod
    async def _subscribe_quietly(local: LocalFeature, remote: RemoteFeature) -> None:
        try:
            await local.subscribe(remote)
        except (SpineError, TimeoutError) as err:
            _LOGGER.debug("subscription to %s failed: %s", remote, err)

    # remote devices
    def setup_remote(self, ski: str, send: Callable[[dict[str, Any]], None]) -> RemoteDevice:
        """Register a connected peer and request its detailed discovery."""
        remote = RemoteDevice(self, ski, Sender(send))
        self.remote_devices[ski] = remote
        self.node_management.read(remote.node_management, FN_DETAILED_DISCOVERY)
        return remote

    def remove_remote(self, ski: str) -> None:
        remote = self.remote_devices.pop(ski, None)
        if remote is None:
            return
        self.subscriptions = [s for s in self.subscriptions
                              if remote.address is None or remote.address not in (s[0].device, s[1].device)]
        self.bindings = [b for b in self.bindings
                         if remote.address is None or remote.address not in (b[0].device, b[1].device)]
        self.publish(Event(EventType.DEVICE, Change.REMOVE, ski, remote))

    def remote_by_address(self, address: str | None) -> RemoteDevice | None:
        return next((d for d in self.remote_devices.values() if d.address == address), None)

    # subscriptions and bindings
    def has_subscription(self, client: Address, server: Address) -> bool:
        return (client, server) in self.subscriptions

    def add_subscription(self, remote: RemoteDevice, client: Address, server: Address) -> None:
        if (client, server) not in self.subscriptions:
            self.subscriptions.append((client, server))
            self.publish(Event(EventType.SUBSCRIPTION, Change.ADD, remote.ski, remote,
                               data={"clientAddress": client, "serverAddress": server}))

    def remove_subscription(self, client: Address, server: Address) -> None:
        with contextlib.suppress(ValueError):
            self.subscriptions.remove((client, server))

    def has_binding(self, client: Address, server: Address) -> bool:
        return (client, server) in self.bindings

    def add_binding(self, remote: RemoteDevice, client: Address, server: Address) -> None:
        if (client, server) not in self.bindings:
            self.bindings.append((client, server))
            self.publish(Event(EventType.BINDING, Change.ADD, remote.ski, remote,
                               data={"clientAddress": client, "serverAddress": server}))

    def notify_subscribers(self, server: Address, cmd: dict[str, Any]) -> None:
        for client, srv in self.subscriptions:
            if srv != server:
                continue
            remote = self.remote_by_address(client.device)
            if remote is not None:
                remote.sender.notify(server, client, cmd)

    # incoming datagrams (spine-go DeviceLocal.ProcessCmd)
    def process_datagram(self, datagram: dict[str, Any], remote: RemoteDevice) -> None:
        header = datagram.get("header") or {}
        cmds = as_list((datagram.get("payload") or {}).get("cmd"))
        if not cmds:
            raise SpineError(ErrorNumber.GENERAL_ERROR, "no payload cmd")
        cmd = cmds[0]
        function, data = cmd_function(cmd)
        classifier = header.get("cmdClassifier")
        source = Address.from_dict(header.get("addressSource"))
        destination = Address.from_dict(header.get("addressDestination"))
        if remote.address is None and source.device:
            remote.address = source.device  # known before the discovery reply arrives
        remote_feature = remote.feature_by_address(source)
        if remote_feature is None:
            _LOGGER.debug("message from unknown remote feature %s", source)
            return
        local_feature = self.feature_by_address(destination)
        if classifier is None or local_feature is None:
            if classifier != CmdClassifier.RESULT:
                remote.sender.result(header, self.address, SpineError(
                    ErrorNumber.DESTINATION_UNKNOWN, "invalid feature address"))
            return
        filters = as_list(cmd.get("filter"))
        msg = Message(header, cmd, classifier, function, data, filters, remote,
                      remote.entity(source.entity), remote_feature)
        _LOGGER.debug("recv %s %s %s -> %s", classifier, function, source, local_feature)

        if classifier == CmdClassifier.WRITE:
            ops = local_feature.operations.get(function or "")
            if ops is None or not ops.write:
                remote.sender.result(header, self.address, SpineError(
                    ErrorNumber.COMMAND_NOT_SUPPORTED, "write not supported for this function"))
                return
            if not self.has_binding(remote_feature.address, local_feature.address):
                remote.sender.result(header, self.address, SpineError(
                    ErrorNumber.BINDING_IS_NECESSARY_FOR_THIS_COMMAND, "write denied: no binding"))
                return

        try:
            if local_feature is self.node_management:
                self._handle_node_management(msg)
            else:
                local_feature.handle(msg)
        except SpineError as err:
            if classifier != CmdClassifier.RESULT:
                remote.sender.result(header, self.address, err)
            if classifier == CmdClassifier.NOTIFY and function:
                local_feature.read(remote_feature, function)  # resync, as spine-go
            return

        if header.get("ackRequest") and classifier in (CmdClassifier.CALL, CmdClassifier.REPLY,
                                                       CmdClassifier.NOTIFY, CmdClassifier.WRITE):
            remote.sender.result(header, self.address)

    # node management (spine-go nodemanagement*.go)
    def _handle_node_management(self, msg: Message) -> None:
        nm = self.node_management
        fn, classifier = msg.function, msg.classifier
        if classifier == CmdClassifier.RESULT or fn == FN_RESULT:
            nm.handle(msg)
        elif fn == FN_DETAILED_DISCOVERY:
            if classifier == CmdClassifier.READ:
                msg.device.sender.reply(msg.header, self.address,
                                        make_cmd(FN_DETAILED_DISCOVERY, self.detailed_discovery()))
            elif classifier == CmdClassifier.REPLY:
                self._discovery_reply(msg)
                nm._resolve(msg.header.get("msgCounterReference"), msg.data)
            elif classifier == CmdClassifier.NOTIFY:
                self._discovery_notify(msg)
            else:
                raise SpineError(ErrorNumber.COMMAND_NOT_SUPPORTED)
        elif fn == FN_USE_CASE:
            if classifier == CmdClassifier.READ:
                msg.device.sender.reply(msg.header, self.address, make_cmd(FN_USE_CASE, nm.get(FN_USE_CASE)))
            elif classifier in (CmdClassifier.REPLY, CmdClassifier.NOTIFY):
                msg.feature.update(FN_USE_CASE, msg.data, msg.filters)
                self.publish(Event(EventType.DATA, Change.UPDATE, msg.device.ski, msg.device,
                                   msg.entity, msg.feature, nm, FN_USE_CASE, classifier,
                                   msg.feature.get(FN_USE_CASE)))
                nm._resolve(msg.header.get("msgCounterReference"), msg.data)
            else:
                raise SpineError(ErrorNumber.COMMAND_NOT_SUPPORTED)
        elif fn == FN_SUBSCRIPTION_REQUEST and classifier == CmdClassifier.CALL:
            req = (msg.data or {}).get("subscriptionRequest") or {}
            client, server = self._fill_addresses(msg, req)
            self._check_server(msg.device, client, server, req.get("serverFeatureType"))
            self.add_subscription(msg.device, client, server)
        elif fn == FN_SUBSCRIPTION_DELETE and classifier == CmdClassifier.CALL:
            req = (msg.data or {}).get("subscriptionDelete") or {}
            client, server = self._fill_addresses(msg, req, delete=True)
            self.subscriptions = [s for s in self.subscriptions if not _matches_delete(s, client, server)]
        elif fn == FN_SUBSCRIPTION and classifier == CmdClassifier.READ:
            entries = [{"subscriptionId": i, "clientAddress": c.to_dict(), "serverAddress": s.to_dict()}
                       for i, (c, s) in enumerate(self.subscriptions)
                       if msg.device.address in (c.device, s.device)]
            msg.device.sender.reply(msg.header, self.address,
                                    make_cmd(FN_SUBSCRIPTION, {"subscriptionEntry": entries}))
        elif fn == FN_BINDING_REQUEST and classifier == CmdClassifier.CALL:
            req = (msg.data or {}).get("bindingRequest") or {}
            client, server = self._fill_addresses(msg, req)
            self._check_server(msg.device, client, server, req.get("serverFeatureType"))
            self.add_binding(msg.device, client, server)
        elif fn == FN_BINDING_DELETE and classifier == CmdClassifier.CALL:
            req = (msg.data or {}).get("bindingDelete") or {}
            client, server = self._fill_addresses(msg, req, delete=True)
            self.bindings = [b for b in self.bindings if not _matches_delete(b, client, server)]
        elif fn == FN_BINDING and classifier == CmdClassifier.READ:
            entries = [{"bindingId": i, "clientAddress": c.to_dict(), "serverAddress": s.to_dict()}
                       for i, (c, s) in enumerate(self.bindings)
                       if msg.device.address in (c.device, s.device)]
            msg.device.sender.reply(msg.header, self.address,
                                    make_cmd(FN_BINDING, {"bindingEntry": entries}))
        elif fn == FN_DESTINATION_LIST and classifier == CmdClassifier.READ:
            entry = {"deviceDescription": {"deviceAddress": {"device": self.address},
                                           "deviceType": self.device_type,
                                           "networkFeatureSet": self.feature_set}}
            msg.device.sender.reply(msg.header, self.address, make_cmd(
                FN_DESTINATION_LIST, {"nodeManagementDestinationData": [entry]}))
        else:
            raise SpineError(ErrorNumber.COMMAND_NOT_SUPPORTED, f"{classifier} {fn} not supported")

    def _fill_addresses(self, msg: Message, req: dict[str, Any],
                        delete: bool = False) -> tuple[Address, Address]:
        client = Address.from_dict(req.get("clientAddress"))
        server = Address.from_dict(req.get("serverAddress"))
        if delete and client.device is None and server.device is None:
            return client.with_device(self.address), server.with_device(msg.device.address)
        if client.device is None:
            client = client.with_device(self.address if delete else msg.device.address)
        if server.device is None:
            server = server.with_device(self.address)
        return client, server

    def _check_server(self, remote: RemoteDevice, client: Address, server: Address,
                      server_type: str | None) -> None:
        if server.device != self.address or client.device != remote.address:
            raise SpineError(ErrorNumber.COMMAND_REJECTED, "invalid addresses")
        local = self.feature_by_address(server)
        remote_feature = remote.feature_by_address(client)
        if local is None or remote_feature is None:
            raise SpineError(ErrorNumber.DESTINATION_UNKNOWN, "feature not found")
        if local.role not in (Role.SERVER, Role.SPECIAL) or remote_feature.role not in (
                Role.CLIENT, Role.SPECIAL):
            raise SpineError(ErrorNumber.COMMAND_REJECTED, "roles do not match")
        if server_type and local.type != server_type:
            raise SpineError(ErrorNumber.COMMAND_REJECTED, "feature type does not match")

    def _discovery_reply(self, msg: Message) -> None:
        remote = msg.device
        data = msg.data or {}
        if not ((data.get("deviceInformation") or {}).get("description")):
            raise SpineError(ErrorNumber.GENERAL_ERROR, "invalid deviceInformation")
        added = remote.update_from_discovery(data, initial=True)
        self.publish(Event(EventType.DEVICE, Change.ADD, remote.ski, remote, data=data))
        for entity in added:
            self.publish(Event(EventType.ENTITY, Change.ADD, remote.ski, remote, entity, data=data))

    def _discovery_notify(self, msg: Message) -> None:
        remote = msg.device
        data = msg.data or {}
        partial, _ = filter_parts(msg.filters)
        infos = as_list(data.get("entityInformation"))
        if partial is None:
            # full notify: work out what changed
            announced = {tuple(as_list(((i.get("description") or {}).get("entityAddress") or {})
                                       .get("entity"))) for i in infos}
            for entity in list(remote.entities):
                if entity.entity not in announced and entity.entity != (0,):
                    self._remove_remote_entity(remote, entity)
            for entity in remote.update_from_discovery(data):
                self.publish(Event(EventType.ENTITY, Change.ADD, remote.ski, remote, entity, data=data))
            return
        for info in infos:
            desc = info.get("description") or {}
            address = tuple(as_list((desc.get("entityAddress") or {}).get("entity")))
            state = desc.get("lastStateChange")
            if state == "added":
                for entity in remote.update_from_discovery(data, only=address):
                    self.publish(Event(EventType.ENTITY, Change.ADD, remote.ski, remote, entity,
                                       data=data))
            elif state == "removed" and (entity := remote.entity(address)) is not None:
                self._remove_remote_entity(remote, entity)

    def _remove_remote_entity(self, remote: RemoteDevice, entity: RemoteEntity) -> None:
        remote.remove_entity(entity.entity)
        self.subscriptions = [s for s in self.subscriptions if not _touches(s, entity)]
        self.bindings = [b for b in self.bindings if not _touches(b, entity)]
        self.publish(Event(EventType.ENTITY, Change.REMOVE, remote.ski, remote, entity))


def _touches(pair: tuple[Address, Address], entity: RemoteEntity) -> bool:
    addr = entity.address
    return any(a.device == addr.device and a.entity == addr.entity for a in pair)


def _matches_delete(pair: tuple[Address, Address], client: Address, server: Address) -> bool:
    c, s = pair
    if c.device != client.device or s.device != server.device:
        return False
    if client.feature is not None:
        return c == client and s == server
    if client.entity:
        return c.entity == client.entity and s.entity == server.entity
    return True
