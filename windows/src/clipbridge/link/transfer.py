"""File transfer service (PROTOCOL.md section 6.2): control messages, data connections, disk I/O.

The link owns one `TransferService`. Pure rules (ranges, hashing, validation) live in
`transfer_core.py`; this module moves the bytes. Disk work, hashing and frame crypto for file
bytes run on worker threads so the event loop (and every clipboard message) never waits on them.

Receiving: `<id>.part` (preallocated) and `<id>.json` (offer, destination, ranges with `have`)
under the partial directory; the `.part` is renamed to the feature's destination once every
range hash matched. Sending: an in-memory FIFO per device, one file at a time (`STREAMS`), whose
unfinished head is re-offered with the same id after a reconnect.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, BinaryIO, Callable

from websockets.exceptions import ConnectionClosed

from clipbridge.link import handshake, transfer_core as core
from clipbridge.link.crypto import FrameError
from clipbridge.link.envelope import EnvelopeError
from clipbridge.link.peers import Peer, TextFrame
from clipbridge.link.transfer_core import CHUNK, FileOffer, Range, RangeStream, TransferViolation

log = logging.getLogger(__name__)

PARTIAL_MAX_AGE = 24 * 3600.0
_LOST = {"type": "lost"}  # local marker: the control connection of an offer closed

SendFn = Callable[[Peer, dict[str, Any]], Awaitable[bool]]
PeerFn = Callable[[str], "Peer | None"]
FeatureFn = Callable[[str], Any]


def _mbps(nbytes: int, seconds: float) -> str:
    return f"{nbytes / 1e6 / max(seconds, 1e-6):.1f} MB/s"


# -- disk helpers (run on worker threads) -------------------------------------


def _hash_region(f: BinaryIO, start: int, length: int, update: Callable[[bytes], None]) -> None:
    f.seek(start)
    while length > 0:
        block = f.read(min(CHUNK, length))
        if not block:
            raise OSError("file shorter than expected")
        update(block)
        length -= len(block)


def _write_at(f: BinaryIO, offset: int, body: bytes, stream: RangeStream) -> None:
    f.seek(offset)
    f.write(body)
    stream.feed(body)


def _read_hashed(f: BinaryIO, n: int, sha: Any) -> bytes:
    block = f.read(n)
    sha.update(block)
    return block


def _create_empty(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb"):
        pass


# -- state ---------------------------------------------------------------------


@dataclass
class Incoming:
    offer: FileOffer
    device: str
    dest: Path
    ranges: list[Range]
    part: Path
    state: Path
    streaming: set[int] = field(default_factory=set)  # range starts with a live data connection
    ended: set[int] = field(default_factory=set)  # range starts whose hash matched
    conns: set[Peer] = field(default_factory=set)

    def save(self) -> None:
        data = {
            "offer": self.offer.to_json(),
            "device": self.device,
            "dest": str(self.dest),
            "ranges": [r.to_wire() for r in self.ranges],
        }
        tmp = self.state.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), "utf-8")
        os.replace(tmp, self.state)


@dataclass
class Outgoing:
    offer: FileOffer
    device: str
    path: Path
    result: asyncio.Future[bool]
    peer: Peer | None = None  # control connection the current offer went out on
    ranges: dict[int, Range] = field(default_factory=dict)  # accepted ranges by start
    streaming: set[int] = field(default_factory=set)
    conns: set[Peer] = field(default_factory=set)
    replies: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    retried: bool = False


class TransferService:
    def __init__(self, send: SendFn, peer_for: PeerFn, feature_for: FeatureFn, partial_dir: Path) -> None:
        self._send = send
        self._peer_for = peer_for
        self._feature_for = feature_for
        self._dir = partial_dir
        self._incoming: dict[str, Incoming] = {}
        self._outgoing: dict[str, Outgoing] = {}  # offers currently out, by id
        self._queues: dict[str, collections.deque[Outgoing]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._data_conns: dict[str, set[Peer]] = {}

    # -- lifecycle -----------------------------------------------------------
    def prune(self, now: float | None = None) -> None:
        """Drop partial files and state older than PARTIAL_MAX_AGE."""
        if not self._dir.is_dir():
            return
        cutoff = (now if now is not None else time.time()) - PARTIAL_MAX_AGE
        for path in self._dir.iterdir():
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    log.info("pruned stale partial %s", path.name)
            except OSError:
                log.warning("could not prune %s", path.name)

    async def stop(self) -> None:
        for task in self._workers.values():
            task.cancel()
        for queue in self._queues.values():
            for item in queue:
                if not item.result.done():
                    item.result.set_result(False)

    async def on_peer_connected(self, peer: Peer) -> None:
        if self._queues.get(peer.device):
            self._ensure_worker(peer.device)

    async def on_peer_disconnected(self, peer: Peer) -> None:
        if self._peer_for(peer.device) is None:  # not replaced by a newer control connection
            for conn in list(self._data_conns.get(peer.device, ())):
                await conn.ws.close(1000)
        for item in list(self._outgoing.values()):
            if item.peer is peer:
                item.replies.put_nowait(_LOST)

    # -- control messages (from the control connection) ----------------------
    async def on_control(self, peer: Peer, header: dict[str, Any]) -> None:
        kind = header.get("type")
        fid = header.get("id")
        if kind == "xfer_offer":
            await self._on_offer(peer, header)
            return
        if not core.valid_id(fid):
            return
        assert isinstance(fid, str)
        item = self._outgoing.get(fid)
        if item is not None and item.peer is peer and kind in ("xfer_accept", "xfer_reject", "xfer_done", "xfer_cancel"):
            item.replies.put_nowait(header)
            return
        inc = self._incoming.get(fid)
        if kind == "xfer_cancel" and inc is not None and inc.device == peer.device:
            log.info("transfer %s cancelled by %s", fid[:8], peer.device)
            await self._discard(inc)
            return
        log.debug("ignored %s for unknown id", kind)

    # -- receiving -----------------------------------------------------------
    def _state_path(self, fid: str) -> Path:
        return self._dir / f"{fid}.json"

    def _load(self, fid: str) -> Incoming | None:
        state = self._state_path(fid)
        try:
            data = json.loads(state.read_text("utf-8"))
            offer = core.parse_offer(data["offer"])
            ranges = core.parse_ranges(data["ranges"], offer.size if offer else -1)
            part = self._dir / f"{fid}.part"
            if offer is None or ranges is None or not part.is_file():
                raise ValueError("bad state")
            return Incoming(offer, str(data["device"]), Path(data["dest"]), ranges, part, state)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("dropping unreadable transfer state %s", fid[:8])
            state.unlink(missing_ok=True)
            (self._dir / f"{fid}.part").unlink(missing_ok=True)
            return None

    async def _on_offer(self, peer: Peer, header: dict[str, Any]) -> None:
        offer = core.parse_offer(header)
        if offer is None:
            log.warning("malformed xfer_offer from %s", peer.device)
            return
        inc = self._incoming.get(offer.id) or await asyncio.to_thread(self._load, offer.id)
        if inc is not None and (inc.device != peer.device or inc.offer.size != offer.size):
            await self._discard(inc)
            inc = None
        if inc is not None:
            self._incoming[offer.id] = inc
            log.info(
                "resume %s (%d bytes, have %d) from %s",
                offer.id[:8], offer.size, sum(r.have for r in inc.ranges), peer.device,
            )
            await self._send(peer, core.accept(offer.id, inc.ranges))
            return

        dest = await self._ask_destination(peer, offer)
        if dest is None:
            await self._send(peer, core.reject(offer.id))
            return
        log.info("incoming %s (%d bytes) from %s for %s", offer.id[:8], offer.size, peer.device, offer.feature)
        if offer.size == 0:
            await self._send(peer, core.accept(offer.id, []))
            await self._finish_empty(peer, offer, dest)
            return
        part = self._dir / f"{offer.id}.part"
        inc = Incoming(offer, peer.device, dest, core.split(offer.size), part, self._state_path(offer.id))
        try:
            await asyncio.to_thread(self._create_part, inc)
        except OSError:
            log.exception("cannot create partial file")
            await self._send(peer, core.reject(offer.id))
            return
        self._incoming[offer.id] = inc
        await self._send(peer, core.accept(offer.id, inc.ranges))

    def _create_part(self, inc: Incoming) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        with open(inc.part, "wb") as f:
            f.truncate(inc.offer.size)
        inc.save()

    async def _ask_destination(self, peer: Peer, offer: FileOffer) -> Path | None:
        feature = self._feature_for(offer.feature)
        if feature is None or not hasattr(feature, "on_file_offer"):
            log.info("no file feature %r; rejecting", offer.feature)
            return None
        try:
            dest = await feature.on_file_offer(peer, offer)
        except Exception:
            log.exception("feature %s failed in on_file_offer", offer.feature)
            return None
        return Path(dest) if dest is not None else None

    async def _finish_empty(self, peer: Peer, offer: FileOffer, dest: Path) -> None:
        while True:
            try:
                await asyncio.to_thread(_create_empty, dest)
                break
            except FileExistsError:
                again = await self._ask_destination(peer, offer)
                if again is None:
                    await self._send(peer, core.done(offer.id, False))
                    return
                dest = again
            except OSError:
                log.exception("cannot create %s", dest.name)
                await self._send(peer, core.done(offer.id, False))
                return
        await self._send(peer, core.done(offer.id, True))
        await self._deliver(peer, offer, dest)

    async def _receive_range(self, conn: Peer, inc: Incoming, rng: Range) -> None:
        stream = RangeStream(rng)
        fid = inc.offer.id
        inc.streaming.add(rng.start)
        inc.conns.add(conn)
        resumed = rng.have
        t0 = time.monotonic()
        try:
            f = await asyncio.to_thread(open, inc.part, "r+b")
            try:
                await asyncio.to_thread(_hash_region, f, rng.start, rng.have, stream.prime)
                while True:
                    header, body = await asyncio.to_thread(conn.open, await conn.ws.recv())
                    if self._incoming.get(fid) is not inc:  # cancelled meanwhile
                        await conn.ws.close(1000)
                        return
                    if core.classify(header) == "end":
                        ok = stream.finish(header)
                        break
                    stream.check(body)
                    await asyncio.to_thread(_write_at, f, stream.position, body, stream)
                    await asyncio.to_thread(inc.save)
            finally:
                await asyncio.to_thread(f.close)
        finally:
            inc.streaming.discard(rng.start)
            inc.conns.discard(conn)
        moved = rng.length - resumed
        log.info("range %s@%d received: %d bytes, %s", fid[:8], rng.start, moved, _mbps(moved, time.monotonic() - t0))
        peer = self._peer_for(inc.device)
        if not ok:
            log.warning("transfer %s: range %d hash mismatch", fid[:8], rng.start)
            await self._discard(inc)
            if peer is not None:
                await self._send(peer, core.done(fid, False))
            return
        inc.ended.add(rng.start)
        if len(inc.ended) == len(inc.ranges):
            await self._finish(inc)

    async def _finish(self, inc: Incoming) -> None:
        fid = inc.offer.id
        self._incoming.pop(fid, None)
        peer = self._peer_for(inc.device)
        dest = inc.dest
        while True:
            try:
                await asyncio.to_thread(self._move, inc.part, dest)
                break
            except FileExistsError:
                again = await self._ask_destination(peer, inc.offer) if peer is not None else None
                if again is None:
                    await self._discard(inc)
                    if peer is not None:
                        await self._send(peer, core.done(fid, False))
                    return
                dest = again
            except OSError:
                log.exception("cannot move %s into place", fid[:8])
                await self._discard(inc)
                if peer is not None:
                    await self._send(peer, core.done(fid, False))
                return
        inc.state.unlink(missing_ok=True)
        log.info("transfer %s complete (%d bytes)", fid[:8], inc.offer.size)
        if peer is not None:
            await self._send(peer, core.done(fid, True))
            await self._deliver(peer, inc.offer, dest)

    @staticmethod
    def _move(part: Path, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.rename(part, dest)  # never overwrites on Windows: FileExistsError

    async def _deliver(self, peer: Peer, offer: FileOffer, path: Path) -> None:
        feature = self._feature_for(offer.feature)
        if feature is None:
            return
        try:
            await feature.on_file_received(peer, offer, path)
        except Exception:
            log.exception("feature %s failed in on_file_received", offer.feature)

    async def _discard(self, inc: Incoming) -> None:
        self._incoming.pop(inc.offer.id, None)
        for conn in list(inc.conns):
            await conn.ws.close(1000)

        def remove() -> None:
            inc.part.unlink(missing_ok=True)
            inc.state.unlink(missing_ok=True)

        try:
            await asyncio.to_thread(remove)
        except OSError:
            log.warning("could not delete partial %s", inc.offer.id[:8])

    # -- sending -------------------------------------------------------------
    async def send_file(self, peer: Peer, feature: str, path: Path, meta: dict[str, Any] | None = None) -> bool:
        try:
            size = (await asyncio.to_thread(path.stat)).st_size
            if not path.is_file():
                raise OSError("not a file")
        except OSError:
            log.warning("send_file: cannot read %s", path.name)
            return False
        offer = FileOffer(os.urandom(16).hex(), feature, core.safe_name(path.name), size, meta or {})
        item = Outgoing(offer, peer.device, path, asyncio.get_running_loop().create_future())
        self._queues.setdefault(peer.device, collections.deque()).append(item)
        log.info("queued %s (%d bytes) for %s", offer.id[:8], size, peer.device)
        self._ensure_worker(peer.device)
        return await item.result

    def _ensure_worker(self, device: str) -> None:
        task = self._workers.get(device)
        if task is None or task.done():
            self._workers[device] = asyncio.create_task(self._run_queue(device))

    async def _run_queue(self, device: str) -> None:
        queue = self._queues[device]
        while queue:
            item = queue[0]
            peer = self._peer_for(device)
            if peer is None:
                return  # on_peer_connected restarts us
            result = await self._offer(peer, item)
            if result is None:  # link lost: re-offer the same id on the next connection
                if self._peer_for(device) is peer:
                    await asyncio.sleep(1.0)
                continue
            queue.popleft()
            if not item.result.done():
                item.result.set_result(result)

    async def _offer(self, peer: Peer, item: Outgoing) -> bool | None:
        """One offer round: True/False when settled, None if the control connection was lost."""
        item.peer = peer
        item.replies = asyncio.Queue()
        item.ranges = {}
        self._outgoing[item.offer.id] = item
        try:
            if not await self._send(peer, item.offer.to_header()):
                return None
            while True:
                reply = await item.replies.get()
                kind = reply.get("type")
                if kind == "lost":
                    return None
                if kind == "xfer_accept":
                    ranges = core.parse_ranges(reply.get("ranges"), item.offer.size)
                    if ranges is None:
                        log.warning("bad xfer_accept ranges for %s; cancelling", item.offer.id[:8])
                        await self._send(peer, core.cancel(item.offer.id))
                        return False
                    item.ranges = {r.start: r for r in ranges}
                elif kind in ("xfer_reject", "xfer_cancel"):
                    log.info("transfer %s %s by %s", item.offer.id[:8], kind[5:], peer.device)
                    return False
                elif kind == "xfer_done":
                    if reply.get("ok") is True:
                        log.info("transfer %s delivered to %s", item.offer.id[:8], peer.device)
                        return True
                    if item.retried:
                        log.warning("transfer %s failed twice; giving up", item.offer.id[:8])
                        return False
                    log.warning("transfer %s failed on %s; offering once more", item.offer.id[:8], peer.device)
                    self._close_conns(item)
                    self._outgoing.pop(item.offer.id, None)
                    item.retried = True
                    item.offer = dataclasses.replace(item.offer, id=os.urandom(16).hex())
                    return await self._offer(peer, item)
        finally:
            if self._outgoing.get(item.offer.id) is item:
                del self._outgoing[item.offer.id]
            self._close_conns(item)

    def _close_conns(self, item: Outgoing) -> None:
        for conn in list(item.conns):
            asyncio.ensure_future(conn.ws.close(1000))

    async def _send_range(self, conn: Peer, item: Outgoing, rng: Range) -> None:
        item.streaming.add(rng.start)
        item.conns.add(conn)
        sha = hashlib.sha256()
        remaining = rng.length - rng.have
        t0 = time.monotonic()
        pending: asyncio.Task[bytes] | None = None
        try:
            f = await asyncio.to_thread(open, item.path, "rb")
            try:
                await asyncio.to_thread(_hash_region, f, rng.start, rng.have, sha.update)
                if remaining > 0:  # read-ahead: the next chunk loads while this one is sent
                    pending = asyncio.ensure_future(asyncio.to_thread(_read_hashed, f, min(CHUNK, remaining), sha))
                while pending is not None:
                    block = await pending
                    pending = None
                    if not block:
                        raise OSError("file shrank while sending")
                    remaining -= len(block)
                    if remaining > 0:
                        pending = asyncio.ensure_future(
                            asyncio.to_thread(_read_hashed, f, min(CHUNK, remaining), sha)
                        )
                    frame = await asyncio.to_thread(conn.seal, core.data(), block)
                    await conn.transmit(frame)
                await conn.transmit(await asyncio.to_thread(conn.seal, core.end(sha.hexdigest())))
            finally:
                if pending is not None:
                    pending.cancel()
                    try:
                        await pending
                    except BaseException:
                        pass
                await asyncio.to_thread(f.close)
        finally:
            item.streaming.discard(rng.start)
            item.conns.discard(conn)
        moved = rng.length - rng.have
        log.info(
            "range %s@%d sent: %d bytes, %s",
            item.offer.id[:8], rng.start, moved, _mbps(moved, time.monotonic() - t0),
        )

    # -- data connections ----------------------------------------------------
    async def serve_data(self, conn: Peer) -> None:
        """Run one authenticated data connection until it closes."""
        conns = self._data_conns.setdefault(conn.device, set())
        conns.add(conn)
        try:
            while True:
                try:
                    header, _ = conn.open(await conn.ws.recv())
                except EnvelopeError:
                    raise TransferViolation("malformed envelope")
                attach = core.parse_attach(header)
                if attach is None:
                    raise TransferViolation("expected xfer_attach")
                fid, start = attach
                inc = self._incoming.get(fid)
                item = self._outgoing.get(fid)
                if inc is not None and inc.device == conn.device:
                    rng = next((r for r in inc.ranges if r.start == start), None)
                    if rng is None or start in inc.streaming or start in inc.ended:
                        break
                    await self._receive_range(conn, inc, rng)
                elif item is not None and item.device == conn.device and start in item.ranges:
                    if start in item.streaming:
                        break
                    await self._send_range(conn, item, item.ranges[start])
                else:
                    break
            log.info("data connection from %s: unknown transfer or range", conn.device)
            await conn.ws.close(handshake.CLOSE_UNKNOWN_TRANSFER)
        except (TransferViolation, TextFrame, EnvelopeError) as exc:
            log.warning("data connection from %s: %s", conn.device, exc)
            await conn.ws.close(handshake.CLOSE_PROTOCOL)
        except FrameError as exc:
            log.warning("data frame integrity failure from %s: %s", conn.device, exc)
            await conn.ws.close(handshake.CLOSE_FRAME, "")
        except ConnectionClosed:
            pass
        finally:
            conns.discard(conn)
