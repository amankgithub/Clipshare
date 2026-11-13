"""WebSocket server: handshake, per-peer encrypted loop, routing by `ch`, send/broadcast, file transfer."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from http import HTTPStatus
from pathlib import Path
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from clipbridge.link import crypto, envelope, handshake, receipts
from clipbridge.link.crypto import CounterExhausted, FrameError, SecureChannel
from clipbridge.link.envelope import EnvelopeError, EnvelopeTooLarge
from clipbridge.link.feature import Feature
from clipbridge.link.handshake import HandshakeError
from clipbridge.link.peers import Peer, PeerRegistry, TextFrame
from clipbridge.link.transfer import TransferService

log = logging.getLogger(__name__)

SUBPROTOCOL = "clipbridge.v1"
DATA_SUBPROTOCOL = "clipbridge.data.v1"
DEFAULT_PORT = 8765
# Pre-auth limits: an unauthenticated peer may hold at most MAX_PENDING_HANDSHAKES slots, each for at most
# AUTH_TIMEOUT seconds in total (challenge + auth), and may send at most handshake.MAX_HANDSHAKE_BYTES.
AUTH_TIMEOUT = 10.0
MAX_PENDING_HANDSHAKES = 16
CLOSE_TRY_AGAIN = 1013
PING_INTERVAL = 10.0
PING_TIMEOUT = 10.0
MAX_MESSAGE = envelope.MAX_PLAINTEXT + crypto.MIN_FRAME_LEN


class Link:
    def __init__(
        self, psk: bytes, partial_dir: Path, host: str = "0.0.0.0", port: int = DEFAULT_PORT
    ) -> None:
        self._psk = psk
        self._host = host
        self._port = port
        self._features: dict[str, Feature] = {}
        self._registry = PeerRegistry()
        self._server: Server | None = None
        self._sending: set[asyncio.Future[bool]] = set()
        self._pending_handshakes = 0
        self._transfers = TransferService(self.send, self._registry.get, self._features.get, partial_dir)

    # -- feature API -------------------------------------------------------
    def register(self, feature: Feature) -> None:
        self._features[feature.name] = feature

    @property
    def peers(self) -> list[Peer]:
        return self._registry.all()

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

    async def broadcast(
        self, header: dict[str, Any], body: bytes = b"", exclude: Peer | None = None
    ) -> None:
        """Queue the message for every peer except `exclude`; returns without waiting for slow peers."""
        for peer in self._registry.all():
            if peer is not exclude:
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
        try:
            peer = await self._handshake(ws)
            if peer is None:
                return
            if ws.subprotocol == DATA_SUBPROTOCOL:
                conn, peer = peer, None  # data connections are not peers: no registry, no notify
                await self._transfers.serve_data(conn)
                return
            old = self._registry.add(peer)
            if old is not None:
                await old.ws.close(handshake.CLOSE_REPLACED)
            log.info("peer connected: %s", peer.device)
            await self._notify("on_peer_connected", peer)
            await self._transfers.on_peer_connected(peer)
            await self._serve_peer(peer)
        except ConnectionClosed:
            pass
        except Exception:
            log.exception("connection handler error")
        finally:
            if peer is not None:
                if self._registry.remove(peer):
                    log.info("peer disconnected: %s", peer.device)
                await self._transfers.on_peer_disconnected(peer)
                await self._notify("on_peer_disconnected", peer)

    async def _handshake(self, ws: ServerConnection) -> Peer | None:
        if self._pending_handshakes >= MAX_PENDING_HANDSHAKES:
            log.warning("too many pending handshakes; refusing connection")
            await ws.close(CLOSE_TRY_AGAIN)
            return None
        self._pending_handshakes += 1
        full_limit = ws.protocol.max_message_size
        ws.protocol.max_message_size = handshake.MAX_HANDSHAKE_BYTES  # enforced while frames are parsed
        try:
            peer = await asyncio.wait_for(self._authenticate(ws), AUTH_TIMEOUT)
        except asyncio.TimeoutError:
            await ws.close(handshake.CLOSE_TIMEOUT)
            return None
        finally:
            self._pending_handshakes -= 1
        ws.protocol.max_message_size = full_limit
        return peer

    async def _authenticate(self, ws: ServerConnection) -> Peer | None:
        sn = os.urandom(16)
        await ws.send(handshake.challenge(sn))
        try:
            auth = handshake.parse_auth(await ws.recv())
        except HandshakeError as exc:
            await ws.close(exc.code)
            return None
        expected = crypto.client_mac(self._psk, sn, auth.cn, auth.device)
        if not hmac.compare_digest(expected, auth.mac):
            log.warning("auth failed (bad MAC)")
            await ws.close(handshake.CLOSE_AUTH, "")
            return None
        await ws.send(handshake.auth_ok(crypto.server_mac(self._psk, sn, auth.cn)))
        c2s, s2c = crypto.derive_keys(self._psk, sn, auth.cn)
        return Peer(auth.device, ws, SecureChannel(send_key=s2c, recv_key=c2s))

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
        if ch.startswith(receipts.LINK_CH):
            kind = header.get("type")
            if ch == receipts.LINK_CH and isinstance(kind, str) and kind.startswith("xfer_"):
                await self._transfers.on_control(peer, header)
            else:
                # The server never asks for receipts, so incoming acks have nothing to resolve.
                log.debug("link control message ch=%r type=%r", ch, kind)
            return
        feature = self._features.get(ch)
        if feature is None:
            log.debug("no feature for ch=%r", ch)
            return
        try:
            await feature.on_message(peer, header, body)
        except Exception:
            log.exception("feature %s failed on message", feature.name)
            return
        mid = receipts.requested_mid(header)
        if mid is not None:  # queued: a relay stalled on this peer must not stop reading from it
            self._queue(peer, receipts.ack(mid))

    async def _notify(self, method: str, peer: Peer) -> None:
        for feature in self._features.values():
            try:
                await getattr(feature, method)(peer)
            except Exception:
                log.exception("feature %s failed in %s", feature.name, method)
