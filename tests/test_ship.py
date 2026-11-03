"""SHIP tests: encoding, certificates, handshake over real TLS websockets."""

from __future__ import annotations

import asyncio

import pytest

from pyeebus.ship import (
    Identity,
    InvalidSkiError,
    ShipError,
    ShipNode,
    State,
    eebus_json,
    model,
    ski_from_certificate,
)
from pyeebus.ship.connection import ShipConnection
from pyeebus.ship.model import Role

# --- EEBUS JSON ------------------------------------------------------------------


def test_eebus_json_hello_matches_spec():
    text = eebus_json.encode({"connectionHello": {"phase": "ready", "waiting": 60000}})
    assert text == '{"connectionHello":[{"phase":"ready"},{"waiting":60000}]}'
    assert eebus_json.decode(text) == {"connectionHello": {"phase": "ready", "waiting": 60000}}


def test_eebus_json_arrays_of_objects_and_empty_object():
    obj = {"cmd": [{"function": "x", "data": {"a": [1, 2]}}], "accessMethodsRequest": {}}
    text = eebus_json.encode(obj)
    assert '"cmd":[[{"function":"x"},{"data":[{"a":[1,2]}]}]]' in text
    assert eebus_json.decode(text) == obj


def test_eebus_json_brackets_in_strings_survive():
    obj = {"x": {"label": "a [{ b }] c"}}
    assert eebus_json.decode(eebus_json.encode(obj)) == obj


def test_data_message_matches_ship_go_layout():
    msg = model.data({"datagram": {"header": {"specificationVersion": "1.3.0"}}})
    assert msg[0] == model.MsgType.DATA
    assert msg[1:].decode() == (
        '{"data":[{"header":[{"protocolId":"ee1.0"}]},'
        '{"payload":{"datagram":[{"header":[{"specificationVersion":"1.3.0"}]}]}}]}'
    )
    kind, body = model.parse(msg)
    assert body["data"]["payload"] == {"datagram": {"header": {"specificationVersion": "1.3.0"}}}


# --- certificates ------------------------------------------------------------------


def test_identity_ski_and_persistence(tmp_path):
    ident = Identity.load_or_create(tmp_path, "test")
    assert len(ident.ski) == 40
    assert ski_from_certificate(ident.cert_der) == ident.ski
    assert Identity.load_or_create(tmp_path, "test").ski == ident.ski


def test_ski_mismatch_rejected():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([])
    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1).not_valid_before(now)
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectKeyIdentifier(b"\x01" * 20), critical=False)
            .sign(key, hashes.SHA256()))
    with pytest.raises(InvalidSkiError):
        ski_from_certificate(cert.public_bytes(serialization.Encoding.DER))


# Elli Gen 1 wallboxes (Skoda iV, VW ID. Charger, firmware 3.9) encode BasicConstraints
# cA TRUE as 0x01 instead of 0xFF, which cryptography refuses (ha-elli #1, #2).
_DER_CA_TRUE = bytes.fromhex("0603551d130101ff040530030101ff")
_BER_CA_TRUE = bytes.fromhex("0603551d130101ff04053003010101")


def _gen1_style(identity: Identity) -> Identity:
    from cryptography import x509

    der = identity.cert_der
    assert der.count(_DER_CA_TRUE) == 1
    der = der.replace(_DER_CA_TRUE, _BER_CA_TRUE)
    with pytest.raises(ValueError, match="BasicConstraints"):
        x509.load_der_x509_certificate(der).extensions  # noqa: B018 - the bug
    import ssl
    return Identity(ssl.DER_cert_to_PEM_cert(der).encode(), identity.key_pem)


def test_ski_of_a_certificate_that_is_not_strict_der():
    ident = Identity.create("gen1")
    expected = ski_from_certificate(ident.cert_der)
    assert ski_from_certificate(_gen1_style(ident).cert_der) == expected


def test_ski_mismatch_rejected_when_not_strict_der():
    a, b = Identity.create("a"), _gen1_style(Identity.create("b"))
    der_b = b.cert_der
    # b's certificate with a's SKI
    swapped = der_b.replace(bytes.fromhex(b.ski), bytes.fromhex(a.ski))
    assert swapped != der_b
    with pytest.raises(InvalidSkiError):
        ski_from_certificate(swapped)


# --- handshake over real TLS websockets ------------------------------------------------


def _node(name: str) -> ShipNode:
    return ShipNode(Identity.create(name), ship_id=f"pyeebus-{name}", brand="pyeebus",
                    model=name, device_type="EnergyManagementSystem", port=0,
                    announce=False, discover=False, host="127.0.0.1")


@pytest.fixture
async def pair():
    server, client = _node("server"), _node("client")
    # Server must know the client's certificate (ssl cannot verify by SKI only).
    server.trust.trust(client.ski, client.identity.cert_pem)
    client.trust.trust(server.ski)
    await server.start()
    await client.start()
    yield server, client
    await client.stop()
    await server.stop()


async def test_handshake_and_data_both_ways(pair):
    server, client = pair
    received_server: list[dict] = []
    received_client: list[dict] = []
    connected = asyncio.Event()

    def on_server_conn(conn: ShipConnection) -> None:
        conn.on_data = received_server.append
        connected.set()

    server.on_connected = on_server_conn
    conn = await client.connect("127.0.0.1", server.port, server.ski)
    conn.on_data = received_client.append
    await asyncio.wait_for(connected.wait(), 5)

    assert conn.state == State.COMPLETE
    assert conn.remote_ship_id == "pyeebus-server"
    srv_conn = server.connections[client.ski]
    assert srv_conn.remote_ship_id == "pyeebus-client"

    await conn.send_spine({"datagram": {"payload": {"cmd": [{"x": 1}]}}})
    await srv_conn.send_spine({"datagram": {"payload": {"cmd": [{"y": 2}]}}})
    for _ in range(50):
        if received_server and received_client:
            break
        await asyncio.sleep(0.02)
    assert received_server == [{"datagram": {"payload": {"cmd": [{"x": 1}]}}}]
    assert received_client == [{"datagram": {"payload": {"cmd": [{"y": 2}]}}}]

    await conn.close()
    await asyncio.wait_for(srv_conn.wait_closed(), 5)
    assert client.ski not in server.connections


async def test_untrusted_or_wrong_ski_rejected(pair):
    server, client = pair
    with pytest.raises(ShipError):
        await client.connect("127.0.0.1", server.port, "0" * 40)  # not trusted
    client.trust.trust("1" * 40)
    with pytest.raises(ShipError, match="expected SKI"):
        await client.connect("127.0.0.1", server.port, "1" * 40)


async def test_unknown_client_certificate_fails_tls(pair):
    server, _ = pair
    stranger = _node("stranger")
    stranger.trust.trust(server.ski)
    with pytest.raises(Exception):  # noqa: B017 - TLS alert surfaces as aiohttp/ssl error
        await stranger.connect("127.0.0.1", server.port, server.ski)


# --- hello phase with a remote that waits for user trust --------------------------------


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.incoming: asyncio.Queue[bytes] = asyncio.Queue()

    async def send(self, message: bytes) -> None:
        self.sent.append(message)

    async def receive(self) -> bytes:
        item = await self.incoming.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self, code: int = 4001, reason: str = "") -> None:
        pass


async def test_hello_prolongation_until_remote_ready(monkeypatch):
    tr = FakeTransport()
    conn = ShipConnection(tr, Role.CLIENT, "local", "a" * 40)
    task = asyncio.create_task(conn.run())
    await tr.incoming.put(model.INIT_MESSAGE)
    await tr.incoming.put(model.hello("pending", prolongation=True))
    await asyncio.sleep(0.05)
    assert conn.state == State.HELLO_WAITING_FOR_TRUST
    last = eebus_json.decode(tr.sent[-1][1:])["connectionHello"]
    assert last["phase"] == "ready" and last["waiting"] > 60000  # prolonged
    await tr.incoming.put(model.hello("ready", 60000))
    await tr.incoming.put(model.protocol_handshake("select"))
    await tr.incoming.put(model.pin_state_none())
    await tr.incoming.put(model.access_methods_request())
    await tr.incoming.put(model.access_methods("remote-id"))
    await asyncio.wait_for(task, 2)
    assert conn.state == State.COMPLETE and conn.remote_ship_id == "remote-id"
    sent_types = [eebus_json.decode(m[1:]) for m in tr.sent[1:] if m[0] == 1]
    assert {"accessMethods": {"id": "local"}} in sent_types


async def test_remote_abort_raises():
    from pyeebus.ship import RemoteAbortError

    tr = FakeTransport()
    conn = ShipConnection(tr, Role.CLIENT, "local", "a" * 40)
    task = asyncio.create_task(conn.run())
    await tr.incoming.put(model.INIT_MESSAGE)
    await tr.incoming.put(model.hello("aborted"))
    with pytest.raises(RemoteAbortError):
        await asyncio.wait_for(task, 2)
    assert conn.state == State.ERROR


async def test_pending_then_close_4452_is_rejection():
    """Elli behaviour for an unpaired node: hello pending, then close 4452."""
    from pyeebus.ship import RemoteAbortError
    from pyeebus.ship.transport import RemoteClosedError

    tr = FakeTransport()
    conn = ShipConnection(tr, Role.CLIENT, "local", "a" * 40)
    task = asyncio.create_task(conn.run())
    await tr.incoming.put(model.INIT_MESSAGE)
    await tr.incoming.put(model.hello("pending", 60000))
    await tr.incoming.put(RemoteClosedError(4452))
    with pytest.raises(RemoteAbortError, match="not paired"):
        await asyncio.wait_for(task, 2)


async def test_data_during_pin_phase_is_kept():
    """ship-go sends SPINE data right after its PIN phase, possibly before we processed it."""
    tr = FakeTransport()
    conn = ShipConnection(tr, Role.CLIENT, "local", "a" * 40)
    received: list[dict] = []
    conn.on_data = received.append
    for message in (model.INIT_MESSAGE, model.hello("ready", 60000), model.protocol_handshake("select"),
                    model.pin_state_none(), model.data({"datagram": {"n": 1}}),
                    model.access_methods_request(), model.access_methods("remote-id"),
                    model.data({"datagram": {"n": 2}})):
        await tr.incoming.put(message)
    await asyncio.wait_for(conn.run(), 2)
    await asyncio.sleep(0.05)
    assert received == [{"datagram": {"n": 1}}, {"datagram": {"n": 2}}]


async def test_handshake_with_a_gen1_style_certificate():
    """The whole connection works when the wallbox's certificate is not strict DER."""
    wallbox = ShipNode(_gen1_style(Identity.create("wallbox")), ship_id="pyeebus-wallbox",
                       brand="pyeebus", model="wallbox", device_type="EVSE", port=0,
                       announce=False, discover=False, host="127.0.0.1")
    cem = _node("cem")
    wallbox.trust.trust(cem.ski, cem.identity.cert_pem)
    cem.trust.trust(wallbox.ski)
    await wallbox.start()
    await cem.start()
    try:
        conn = await cem.connect("127.0.0.1", wallbox.port, wallbox.ski)
        assert conn.state == State.COMPLETE
        await conn.close()
    finally:
        await cem.stop()
        await wallbox.stop()
