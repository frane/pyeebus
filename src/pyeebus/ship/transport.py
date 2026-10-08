"""TLS websocket transport for SHIP (SHIP 9, 10) on top of aiohttp."""

from __future__ import annotations

import contextlib
import ssl
import tempfile
from pathlib import Path

import aiohttp
from aiohttp import web

from .cert import CIPHERS, Identity, ski_from_certificate
from .model import PING_INTERVAL, SUBPROTOCOL

MAX_MESSAGE_SIZE = 100 * 1024


def _load_identity(context: ssl.SSLContext, identity: Identity) -> None:
    # ssl can only load cert/key from files.
    with tempfile.TemporaryDirectory() as tmp:
        cert_file, key_file = Path(tmp, "cert.pem"), Path(tmp, "key.pem")
        cert_file.write_bytes(identity.cert_pem)
        key_file.write_bytes(identity.key_pem)
        context.load_cert_chain(cert_file, key_file)


def client_context(identity: Identity) -> ssl.SSLContext:
    """Client side: present our certificate; the peer is checked by SKI afterwards
    (SHIP 12.1: certificates are self-signed, there is no chain to verify)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.set_ciphers(CIPHERS)
    _load_identity(context, identity)
    return context


def server_context(identity: Identity, trusted_certs_pem: list[bytes]) -> ssl.SSLContext:
    """Server side: require a client certificate (SHIP 9).

    Python's ssl cannot request a client certificate without verifying it, so
    the certificates of trusted peers are loaded as trust anchors (they are
    self-signed CA certificates). Unknown peers fail the TLS handshake; we
    reach them as client instead. Add peers later with add_trusted_cert().
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.set_ciphers(CIPHERS)
    context.options |= ssl.OP_NO_TICKET  # SHIP 9.6
    _load_identity(context, identity)
    for pem in trusted_certs_pem:
        add_trusted_cert(context, pem)
    return context


def add_trusted_cert(context: ssl.SSLContext, pem: bytes) -> None:
    with contextlib.suppress(ssl.SSLError):  # already loaded
        context.load_verify_locations(cadata=pem.decode())


def peer_certificate(ssl_object: ssl.SSLObject | ssl.SSLSocket | None) -> bytes:
    der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
    if not der:
        raise ConnectionError("peer did not present a certificate")
    return der


class AiohttpTransport:
    """SHIP Transport over an aiohttp websocket (client or server side)."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse | web.WebSocketResponse,
                 session: aiohttp.ClientSession | None = None) -> None:
        self._ws = ws
        self._session = session

    async def send(self, message: bytes) -> None:
        if self._ws.closed:
            raise ConnectionError("websocket closed")
        await self._ws.send_bytes(message)

    async def receive(self) -> bytes:
        while True:
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.BINARY:
                if len(msg.data) < 2:
                    raise ConnectionError("SHIP message too short")
                return msg.data
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED):
                raise ConnectionError(f"websocket closed ({self._ws.close_code})")
            if msg.type == aiohttp.WSMsgType.ERROR:
                raise ConnectionError(f"websocket error: {self._ws.exception()}")
            # text frames are not allowed in SHIP; ignore pings handled by aiohttp

    async def close(self, code: int = 4001, reason: str = "") -> None:
        with contextlib.suppress(Exception):
            await self._ws.close(code=code, message=reason.encode())
        if self._session is not None:
            await self._session.close()


async def connect(host: str, port: int, path: str, identity: Identity,
                  timeout: float = 10.0) -> tuple[AiohttpTransport, bytes]:
    """Open a SHIP websocket to a remote node. Returns transport and peer cert (DER)."""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # IPv6 literal
    url = f"wss://{host}:{port}{path or '/ship/'}"
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=timeout))
    try:
        ws = await session.ws_connect(
            url, ssl=client_context(identity), protocols=(SUBPROTOCOL,),
            heartbeat=PING_INTERVAL, max_msg_size=MAX_MESSAGE_SIZE, autoping=True,
        )
    except Exception:
        await session.close()
        raise
    if ws.protocol != SUBPROTOCOL:
        await ws.close()
        await session.close()
        raise ConnectionError(f"remote did not accept subprotocol 'ship' ({ws.protocol})")
    der = peer_certificate(ws.get_extra_info("ssl_object"))
    return AiohttpTransport(ws, session), der


async def accept(request: web.Request) -> tuple[web.WebSocketResponse, AiohttpTransport, str]:
    """Upgrade an incoming request to a SHIP websocket; returns ws, transport, remote SKI."""
    der = peer_certificate(request.transport.get_extra_info("ssl_object"))
    ski = ski_from_certificate(der)
    ws = web.WebSocketResponse(protocols=(SUBPROTOCOL,), heartbeat=PING_INTERVAL,
                               max_msg_size=MAX_MESSAGE_SIZE)
    await ws.prepare(request)
    if ws.ws_protocol != SUBPROTOCOL:
        await ws.close(code=4001, message=b"subprotocol ship required")
        raise ConnectionError("remote did not request subprotocol 'ship'")
    return ws, AiohttpTransport(ws), ski
