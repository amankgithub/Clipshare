"""WebSocket server: handshake, per-peer encrypted loop, routing by `ch`, send/broadcast."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from http import HTTPStatus
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
from clipbridge.link.peers import Peer, PeerRegistry

log = logging.getLogger(__name__)

SUBPROTOCOL = "clipbridge.v1"
DEFAULT_PORT = 8765
AUTH_TIMEOUT = 10.0
PING_INTERVAL = 10.0
PING_TIMEOUT = 10.0
MAX_MESSAGE = envelope.MAX_PLAINTEXT + crypto.MIN_FRAME_LEN


class Link:
    def __init__(self, psk: bytes, host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> None:
        self._psk = psk
        self._host = host
        self._port = port
        self._features: dict[str, Feature] = {}
        self._registry = PeerRegistry()
        self._server: Server | None = None

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
        targets = [p for p in self._registry.all() if p is not exclude]
        await asyncio.gather(*(self.send(p, header, body) for p in targets))

    # -- lifecycle ---------------------------------------------------------
    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self._server = await serve(
            self._handle,
            self._host,
            self._port,
            subprotocols=[SUBPROTOCOL],  # type: ignore[list-item]
            process_request=self._process_request,
            ping_interval=PING_INTERVAL,
            ping_timeout=PING_TIMEOUT,
            max_size=MAX_MESSAGE,
        )
        log.info("link listening on %s:%s", self._host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close(close_connections=True)
            await self._server.wait_closed()
            self._server = None

    # -- internals ---------------------------------------------------------
    @staticmethod
    def _process_request(connection: ServerConnection, request: Request) -> Response | None:
        offered = request.headers.get("Sec-WebSocket-Protocol", "")
        if SUBPROTOCOL not in [p.strip() for p in offered.split(",")]:
            return connection.respond(HTTPStatus.BAD_REQUEST, "subprotocol required\n")
        return None

    async def _handle(self, ws: ServerConnection) -> None:
        peer: Peer | None = None
        try:
            peer = await self._handshake(ws)
            if peer is None:
                return
            old = self._registry.add(peer)
            if old is not None:
                await old.ws.close(handshake.CLOSE_REPLACED)
            log.info("peer connected: %s", peer.device)
            await self._notify("on_peer_connected", peer)
            await self._serve_peer(peer)
        except ConnectionClosed:
            pass
        except Exception:
            log.exception("connection handler error")
        finally:
            if peer is not None:
                if self._registry.remove(peer):
                    log.info("peer disconnected: %s", peer.device)
                await self._notify("on_peer_disconnected", peer)

    async def _handshake(self, ws: ServerConnection) -> Peer | None:
        sn = os.urandom(16)
        await ws.send(handshake.challenge(sn))
        try:
            message = await asyncio.wait_for(ws.recv(), AUTH_TIMEOUT)
            auth = handshake.parse_auth(message)
        except asyncio.TimeoutError:
            await ws.close(handshake.CLOSE_TIMEOUT)
            return None
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
            if isinstance(message, str):
                await peer.ws.close(handshake.CLOSE_PROTOCOL)
                return
            try:
                plaintext = peer.channel.decrypt(message)
            except FrameError as exc:
                log.warning("frame integrity failure from %s: %s", peer, exc)
                await peer.ws.close(handshake.CLOSE_FRAME, "")
                return
            try:
                header, body = envelope.decode(plaintext)
            except EnvelopeError as exc:
                log.warning("malformed envelope from %s: %s", peer, exc)
                continue
            await self._route(peer, header, body)

    async def _route(self, peer: Peer, header: dict[str, Any], body: bytes) -> None:
        ch = header["ch"]
        if ch.startswith(receipts.LINK_CH):
            # The server never asks for receipts, so incoming acks have nothing to resolve.
            log.debug("link control message ch=%r type=%r", ch, header.get("type"))
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
        if mid is not None:
            await self.send(peer, receipts.ack(mid))

    async def _notify(self, method: str, peer: Peer) -> None:
        for feature in self._features.values():
            try:
                await getattr(feature, method)(peer)
            except Exception:
                log.exception("feature %s failed in %s", feature.name, method)
