"""WebSocket server: handshake, hello, per-peer encrypted loop, routing by `ch`, send/broadcast/request, file transfer."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import socket
from http import HTTPStatus
from pathlib import Path
from typing import Any, Protocol

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from clipbridge.link import crypto, envelope, handshake, hello, requests
from clipbridge.link.crypto import CounterExhausted, FrameError, SecureChannel
from clipbridge.link.envelope import LINK_CH, EnvelopeError, EnvelopeTooLarge
from clipbridge.link.feature import Feature
from clipbridge.link.handshake import HandshakeError
from clipbridge.link.peers import Peer, PeerRegistry, TextFrame
from clipbridge.link.requests import Answer, Reply
from clipbridge.link.transfer import TransferService

log = logging.getLogger(__name__)

SUBPROTOCOL = "clipbridge.v1"
DATA_SUBPROTOCOL = "clipbridge.data.v1"
DEFAULT_PORT = 8765
# Pre-auth limits: an unauthenticated peer may hold at most MAX_PENDING_HANDSHAKES slots, each for at most
# AUTH_TIMEOUT seconds in total (challenge + auth), and may send at most handshake.MAX_HANDSHAKE_BYTES.
AUTH_TIMEOUT = 10.0
HELLO_TIMEOUT = 10.0  # after auth_ok, for the client's hello
MAX_PENDING_HANDSHAKES = 16
CLOSE_TRY_AGAIN = 1013
PING_INTERVAL = 10.0
PING_TIMEOUT = 10.0
MAX_MESSAGE = envelope.MAX_PLAINTEXT + crypto.MIN_FRAME_LEN


class Devices(Protocol):
    """The device registry as the link sees it (PROTOCOL.md section 2). Read on every handshake."""

    def psk(self, device: str) -> bytes | None: ...

    def set_name(self, device: str, name: str) -> None: ...


class Link:
    def __init__(
        self,
        devices: Devices,
        partial_dir: Path,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        name: str | None = None,
    ) -> None:
        self._devices = devices
        self._name = hello.clean_name(name or socket.gethostname())
        self._host = host
        self._port = port
        self._features: dict[str, Feature] = {}
        self._registry = PeerRegistry()
        self._server: Server | None = None
        self._sending: set[asyncio.Future[bool]] = set()
        self._pending_handshakes = 0
        self._conns: dict[str, set[ServerConnection]] = {}  # every authenticated connection, by device
        self._transfers = TransferService(self.send, self._registry.get, self._features.get, partial_dir)

    # -- feature API -------------------------------------------------------
    def register(self, feature: Feature) -> None:
        self._features[feature.name] = feature

    @property
    def peers(self) -> list[Peer]:
        """Control connections whose hello has arrived."""
        return [p for p in self._registry.all() if p.features is not None]

    async def send(self, peer: Peer, header: dict[str, Any], body: bytes = b"") -> bool:
        try:
            await peer.send(header, body)
            return True
        except EnvelopeTooLarge as exc:
            log.warning("skip oversize envelope (%s bytes) to %s", exc, peer)
        except CounterExhausted:
            await peer.ws.close(1000)
        except ConnectionClosed:
            log.debug("send to closed peer %s", peer)
        except Exception:
            log.exception("send to %s failed", peer)
        return False

    async def request(self, peer: Peer, header: dict[str, Any], body: bytes = b"") -> Reply | None:
        mid, reply = peer.requests.open()
        try:
            if not await self.send(peer, {**header, "mid": mid}, body):
                return None
            return await asyncio.wait_for(reply, requests.REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            log.info("request %s to %s: no reply", mid, peer)
            return None
        finally:
            peer.requests.close(mid)

    async def broadcast(
        self, header: dict[str, Any], body: bytes = b"", exclude: Peer | None = None
    ) -> None:
        """Queue the message for every peer that has its feature; returns without waiting for slow peers."""
        for peer in self.peers:
            if peer is not exclude and peer.supports(header["ch"]):
                self._queue(peer, header, body)

    def _queue(self, peer: Peer, header: dict[str, Any], body: bytes = b"") -> None:
        """Send in the background. Queued messages reach each peer in queue order (its send lock is FIFO)."""
        task = asyncio.ensure_future(self.send(peer, header, body))
        self._sending.add(task)
        task.add_done_callback(self._sending.discard)

    async def send_file(
        self, peer: Peer, feature: str, path: Path, meta: dict[str, Any] | None = None
    ) -> bool:
        return await self._transfers.send_file(peer, feature, path, meta)

    # -- device registry -----------------------------------------------------
    async def revoke(self, device: str) -> None:
        """`device` was unpaired: close all its authenticated connections, control and data, with 4401.

        Covers connections still waiting for hello or not yet attached to a transfer.
        """
        conns = sorted(self._conns.get(device, ()), key=lambda ws: ws.subprotocol == SUBPROTOCOL)
        for ws in conns:  # data first: losing the control connection would close them with 1000
            await ws.close(handshake.CLOSE_AUTH, "")

    # -- lifecycle ---------------------------------------------------------
    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        await asyncio.to_thread(self._transfers.prune)
        self._server = await serve(
            self._handle,
            self._host,
            self._port,
            subprotocols=[SUBPROTOCOL, DATA_SUBPROTOCOL],  # type: ignore[list-item]
            process_request=self._process_request,
            ping_interval=PING_INTERVAL,
            ping_timeout=PING_TIMEOUT,
            max_size=MAX_MESSAGE,
            compression=None,  # frames are ciphertext: deflate only burns CPU
        )
        log.info("link listening on %s:%s", self._host, self.port)

    async def stop(self) -> None:
        await self._transfers.stop()
        for task in list(self._sending):
            task.cancel()
        await asyncio.gather(*self._sending, return_exceptions=True)
        if self._server is not None:
            self._server.close(close_connections=True)
            await self._server.wait_closed()
            self._server = None

    # -- internals ---------------------------------------------------------
    @staticmethod
    def _process_request(connection: ServerConnection, request: Request) -> Response | None:
        offered = {p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",")}
        if not offered & {SUBPROTOCOL, DATA_SUBPROTOCOL}:
            return connection.respond(HTTPStatus.BAD_REQUEST, "subprotocol required\n")
        return None

    async def _handle(self, ws: ServerConnection) -> None:
        peer: Peer | None = None
        device: str | None = None
        connected = False
        try:
            result = await self._handshake(ws)
            if result is None:
                return
            authed, psk = result
            device = authed.device
            self._conns.setdefault(device, set()).add(ws)  # from here on, revoke() reaches it
            if await asyncio.to_thread(self._devices.psk, device) != psk:  # unpaired/re-keyed meanwhile
                log.info("%s was unpaired during its handshake", device)
                await ws.close(handshake.CLOSE_AUTH, "")
                return
            if ws.subprotocol == DATA_SUBPROTOCOL:  # data connections are not peers: no registry, no hello
                await self._transfers.serve_data(authed)
                return
            peer = authed
            await peer.send(hello.encode(self._name, {f.name: f.version for f in self._features.values()}))
            if not await self._receive_hello(peer):
                return
            old = self._registry.add(peer)
            if old is not None:
                await old.ws.close(handshake.CLOSE_REPLACED)
            connected = True
            log.info("peer connected: %s (features %s)", peer.device, sorted(peer.features or ()))
            await self._notify("on_peer_connected", peer)
            await self._transfers.on_peer_connected(peer)
            await self._serve_peer(peer)
        except ConnectionClosed:
            pass
        except Exception:
            log.exception("connection handler error")
        finally:
            if device is not None:
                conns = self._conns.get(device, set())
                conns.discard(ws)
                if not conns:
                    self._conns.pop(device, None)
            if peer is not None:
                peer.requests.fail_all()
                if self._registry.remove(peer):
                    log.info("peer disconnected: %s", peer.device)
                await self._transfers.on_peer_disconnected(peer)
                if connected:
                    await self._notify("on_peer_disconnected", peer)

    async def _handshake(self, ws: ServerConnection) -> tuple[Peer, bytes] | None:
        """The authenticated connection and the PSK it authenticated with, or None (closed)."""
        if self._pending_handshakes >= MAX_PENDING_HANDSHAKES:
            log.warning("too many pending handshakes; refusing connection")
            await ws.close(CLOSE_TRY_AGAIN)
            return None
        self._pending_handshakes += 1
        full_limit = ws.protocol.max_message_size
        ws.protocol.max_message_size = handshake.MAX_HANDSHAKE_BYTES  # enforced while frames are parsed
        try:
            result = await asyncio.wait_for(self._authenticate(ws), AUTH_TIMEOUT)
        except asyncio.TimeoutError:
            await ws.close(handshake.CLOSE_TIMEOUT)
            return None
        finally:
            self._pending_handshakes -= 1
        ws.protocol.max_message_size = full_limit
        return result

    async def _authenticate(self, ws: ServerConnection) -> tuple[Peer, bytes] | None:
        sn = os.urandom(16)
        await ws.send(handshake.challenge(sn))
        try:
            auth = handshake.parse_auth(await ws.recv())
        except HandshakeError as exc:
            await ws.close(exc.code)
            return None
        psk = await asyncio.to_thread(self._devices.psk, auth.device)  # re-read: sees pair/unpair at once
        if psk is None:
            log.warning("auth failed (unknown device %s)", auth.device)
            await ws.close(handshake.CLOSE_AUTH, "")
            return None
        expected = crypto.client_mac(psk, sn, auth.cn, auth.device)
        if not hmac.compare_digest(expected, auth.mac):
            log.warning("auth failed (bad MAC from %s)", auth.device)
            await ws.close(handshake.CLOSE_AUTH, "")
            return None
        await ws.send(handshake.auth_ok(crypto.server_mac(psk, sn, auth.cn)))
        c2s, s2c = crypto.derive_keys(psk, sn, auth.cn)
        return Peer(auth.device, ws, SecureChannel(send_key=s2c, recv_key=c2s)), psk

    async def _receive_hello(self, peer: Peer) -> bool:
        """Read the client's first frame, which must be a valid hello with an empty body (else close 4400).

        It must arrive within HELLO_TIMEOUT (else close 4408).
        """
        try:
            header, body = peer.open(await asyncio.wait_for(peer.ws.recv(), HELLO_TIMEOUT))
        except asyncio.TimeoutError:
            log.warning("%s sent no hello", peer)
            await peer.ws.close(handshake.CLOSE_TIMEOUT)
            return False
        except FrameError as exc:
            log.warning("frame integrity failure from %s: %s", peer, exc)
            await peer.ws.close(handshake.CLOSE_FRAME, "")
            return False
        except (TextFrame, EnvelopeError):
            header, body = {}, b""
        received = hello.parse(header) if not body else None
        if received is None:
            log.warning("%s: first frame is not a valid hello", peer)
            await peer.ws.close(handshake.CLOSE_PROTOCOL)
            return False
        peer.name, peer.features = received.name, received.features
        try:
            await asyncio.to_thread(self._devices.set_name, peer.device, received.name)
        except Exception:
            log.exception("storing the name of %s failed", peer.device)
        return True

    async def _serve_peer(self, peer: Peer) -> None:
        async for message in peer.ws:
            try:
                header, body = peer.open(message)
            except TextFrame:
                await peer.ws.close(handshake.CLOSE_PROTOCOL)
                return
            except FrameError as exc:
                log.warning("frame integrity failure from %s: %s", peer, exc)
                await peer.ws.close(handshake.CLOSE_FRAME, "")
                return
            except EnvelopeError as exc:
                log.warning("malformed envelope from %s: %s", peer, exc)
                continue
            await self._route(peer, header, body)

    async def _route(self, peer: Peer, header: dict[str, Any], body: bytes) -> None:
        ch = header["ch"]
        if ch.startswith(LINK_CH):
            await self._on_link(peer, header, body)
            return
        mid = requests.requested_mid(header)
        if mid is None and "mid" in header:
            log.warning("invalid mid from %s; message dropped", peer)
            return
        ok, answer = False, None
        feature = self._features.get(ch)
        if feature is None:
            log.debug("no feature for ch=%r", ch)
        else:
            try:
                answer = await feature.on_message(peer, header, body)
                ok = True
            except Exception:
                log.exception("feature %s failed on message", feature.name)
        if mid is not None:
            self._reply(peer, mid, ok, answer)

    def _reply(self, peer: Peer, mid: str, ok: bool, answer: Answer | None) -> None:
        header, body = requests.reply(mid, ok, answer)
        try:
            envelope.encode(header, body)
        except EnvelopeTooLarge:
            log.warning("reply %s to %s too large; answering ok:false", mid, peer)
            header, body = requests.reply(mid, False)
        self._queue(peer, header, body)  # queued: a relay stalled on this peer must not stop reading from it

    async def _on_link(self, peer: Peer, header: dict[str, Any], body: bytes) -> None:
        kind = header.get("type")
        if header["ch"] != LINK_CH:
            log.debug("reserved ch=%r ignored", header["ch"])
        elif kind == "hello":
            log.warning("%s sent a second hello", peer)
            await peer.ws.close(handshake.CLOSE_PROTOCOL)
        elif kind == "reply":
            parsed = requests.parse_reply(header, body)
            if parsed is not None:
                peer.requests.resolve(*parsed)
        elif isinstance(kind, str) and kind.startswith("xfer_"):
            await self._transfers.on_control(peer, header)
        else:
            log.debug("link message type=%r ignored", kind)

    async def _notify(self, method: str, peer: Peer) -> None:
        for feature in self._features.values():
            try:
                await getattr(feature, method)(peer)
            except Exception:
                log.exception("feature %s failed in %s", feature.name, method)
