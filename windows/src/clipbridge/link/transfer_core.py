"""File transfer (PROTOCOL.md section 6.2): pure logic, no I/O.

Message builders/parsers, range split and resume bookkeeping, per-range streaming checks and
SHA-256, and file-name sanitising. The socket and disk side lives in `transfer.py`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from clipbridge.link.envelope import LINK_CH

STREAMS = 1  # data connections per transfer; raised only if the benchmark shows a gain
CHUNK = 4 * 1024 * 1024  # max xfer_data body

_ID = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}


class TransferViolation(Exception):
    """Data connection broke the stream rules (close 4400)."""


def valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def safe_name(name: str) -> str:
    """Base name that is safe to create on Windows; never empty, never a path."""
    base = re.split(r"[\\/]", name)[-1]
    base = _FORBIDDEN.sub("_", base).rstrip(" .").lstrip(" ")
    if not base or base in (".", ".."):
        base = "file"
    if base.split(".")[0].upper() in _RESERVED:
        base = "_" + base
    if len(base) > 255:
        stem, dot, ext = base.rpartition(".")
        base = (stem[: 255 - len(ext) - 1] + "." + ext) if dot and len(ext) < 32 else base[:255]
    return base


# -- offer ---------------------------------------------------------------------


@dataclass(frozen=True)
class FileOffer:
    """A file one side wants to give the other. `name` is already a safe base name."""

    id: str
    feature: str
    name: str
    size: int
    meta: dict[str, Any] = field(default_factory=dict)

    def to_header(self) -> dict[str, Any]:
        return {
            "ch": LINK_CH,
            "type": "xfer_offer",
            "id": self.id,
            "feature": self.feature,
            "name": self.name,
            "size": self.size,
            "meta": self.meta,
        }

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "feature": self.feature, "name": self.name, "size": self.size, "meta": self.meta}


def parse_offer(header: dict[str, Any]) -> FileOffer | None:
    """Validated offer, or None if any field is missing or ill-typed."""
    fid, feature, name, size, meta = (header.get(k) for k in ("id", "feature", "name", "size", "meta"))
    if not valid_id(fid) or not isinstance(feature, str) or not feature:
        return None
    if not isinstance(name, str) or not 1 <= len(name) <= 255:
        return None
    if not _is_int(size) or size < 0 or not isinstance(meta, dict):
        return None
    assert isinstance(fid, str)
    return FileOffer(fid, feature, safe_name(name), size, meta)


# -- ranges --------------------------------------------------------------------


@dataclass
class Range:
    """Bytes [start, end) of a file, of which the first `have` are already on the receiver's disk."""

    start: int
    end: int
    have: int = 0

    @property
    def length(self) -> int:
        return self.end - self.start

    def to_wire(self) -> list[int]:
        return [self.start, self.end, self.have]


def split(size: int, parts: int = STREAMS) -> list[Range]:
    """Split [0, size) into up to `parts` contiguous ranges (none for an empty file)."""
    if size == 0:
        return []
    parts = max(1, min(parts, size))
    step, extra = divmod(size, parts)
    ranges: list[Range] = []
    start = 0
    for i in range(parts):
        end = start + step + (1 if i < extra else 0)
        ranges.append(Range(start, end))
        start = end
    return ranges


def parse_ranges(raw: object, size: int) -> list[Range] | None:
    """Validate an `xfer_accept` ranges list: sorted, non-overlapping, covering [0, size)."""
    if not isinstance(raw, list):
        return None
    ranges: list[Range] = []
    pos = 0
    for item in raw:
        if not isinstance(item, list) or len(item) != 3 or not all(_is_int(v) for v in item):
            return None
        start, end, have = item
        if start != pos or end <= start or not 0 <= have <= end - start:
            return None
        ranges.append(Range(start, end, have))
        pos = end
    return ranges if pos == size else None


def full_ranges(size: int) -> list[Range]:
    """The split of a file the receiver already completed (`have` = length everywhere)."""
    return [Range(r.start, r.end, r.length) for r in split(size)]


def rewind_full(ranges: list[Range]) -> None:
    """Before resuming an unfinished file: a range fully on disk is not yet verified, so report it
    one byte short; the sender then streams that byte and `xfer_end`, and the hash is checked."""
    for r in ranges:
        r.have = min(r.have, r.length - 1)


# -- completed ids -------------------------------------------------------------

COMPLETED_MAX = 256
COMPLETED_MAX_AGE = 24 * 3600.0


@dataclass(frozen=True)
class _Completed:
    device: str
    size: int
    at: float


class CompletedIds:
    """Ids this receiver finished with `ok:true`, so a re-offer after a lost `xfer_done` is
    answered `ok:true` again instead of being received twice. Bounded by count and age; times are
    wall-clock seconds so the record can be saved and reloaded (`to_json` / `from_json`)."""

    def __init__(self, max_items: int = COMPLETED_MAX, max_age: float = COMPLETED_MAX_AGE) -> None:
        self._max_items = max_items
        self._max_age = max_age
        self._items: dict[str, _Completed] = {}  # insertion order = completion order

    def add(self, fid: str, device: str, size: int, now: float) -> None:
        self._items.pop(fid, None)
        self._items[fid] = _Completed(device, size, now)
        self._prune(now)

    def has(self, fid: str, device: str, size: int, now: float) -> bool:
        self._prune(now)
        item = self._items.get(fid)
        return item is not None and item.device == device and item.size == size

    def _prune(self, now: float) -> None:
        while self._items:
            oldest = next(iter(self._items))
            if len(self._items) <= self._max_items and now - self._items[oldest].at <= self._max_age:
                break
            del self._items[oldest]

    def __len__(self) -> int:
        return len(self._items)

    def to_json(self) -> list[list[Any]]:
        return [[fid, c.device, c.size, c.at] for fid, c in self._items.items()]

    @classmethod
    def from_json(cls, raw: object, now: float) -> "CompletedIds":
        """Rebuild from `to_json` output; malformed entries are skipped."""
        ids = cls()
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, list) or len(item) != 4:
                continue
            fid, device, size, at = item
            if valid_id(fid) and isinstance(device, str) and _is_int(size) and isinstance(at, (int, float)):
                ids._items[fid] = _Completed(device, size, float(at))
        ids._items = dict(sorted(ids._items.items(), key=lambda kv: kv[1].at))
        ids._prune(now)
        return ids


def accept(fid: str, ranges: list[Range]) -> dict[str, Any]:
    return {"ch": LINK_CH, "type": "xfer_accept", "id": fid, "ranges": [r.to_wire() for r in ranges]}


def reject(fid: str) -> dict[str, Any]:
    return {"ch": LINK_CH, "type": "xfer_reject", "id": fid}


def done(fid: str, ok: bool) -> dict[str, Any]:
    return {"ch": LINK_CH, "type": "xfer_done", "id": fid, "ok": ok}


def cancel(fid: str) -> dict[str, Any]:
    return {"ch": LINK_CH, "type": "xfer_cancel", "id": fid}


def attach(fid: str, start: int) -> dict[str, Any]:
    return {"ch": LINK_CH, "type": "xfer_attach", "id": fid, "start": start}


def data() -> dict[str, Any]:
    return {"ch": LINK_CH, "type": "xfer_data"}


def end(sha256_hex: str) -> dict[str, Any]:
    return {"ch": LINK_CH, "type": "xfer_end", "sha256": sha256_hex}


def parse_attach(header: dict[str, Any]) -> tuple[str, int] | None:
    fid, start = header.get("id"), header.get("start")
    if header.get("ch") != LINK_CH or header.get("type") != "xfer_attach":
        return None
    if not valid_id(fid) or not _is_int(start):
        return None
    assert isinstance(fid, str) and isinstance(start, int)
    return fid, start


# -- streaming one range -------------------------------------------------------


class RangeStream:
    """Receiver-side checks for one range: positions, size limits, and the running SHA-256.

    Feed it the `have` prefix read back from disk (`prime`), then each `xfer_data` body (`feed`),
    then the `xfer_end` header (`finish`). The caller writes bytes at `position` before feeding.
    """

    def __init__(self, rng: Range) -> None:
        self.range = rng
        self.position = rng.start + rng.have  # absolute offset of the next byte to arrive
        self._sha = hashlib.sha256()

    def prime(self, prefix: bytes) -> None:
        self._sha.update(prefix)

    def check(self, body: bytes) -> None:
        """Raise if this body may not be written at `position`."""
        if not 1 <= len(body) <= CHUNK:
            raise TransferViolation("bad chunk size")
        if self.position + len(body) > self.range.end:
            raise TransferViolation("bytes past end of range")

    def feed(self, body: bytes) -> None:
        self.check(body)
        self._sha.update(body)
        self.position += len(body)
        self.range.have = self.position - self.range.start

    def finish(self, header: dict[str, Any]) -> bool:
        """Return whether the range hash matches. Raise if the end arrived early or is malformed."""
        if self.position != self.range.end:
            raise TransferViolation("xfer_end before end of range")
        digest = header.get("sha256")
        if not isinstance(digest, str) or _HEX64.fullmatch(digest) is None:
            raise TransferViolation("bad sha256")
        return digest == self._sha.hexdigest()


def classify(header: dict[str, Any]) -> str:
    """Type of a frame received on a data connection mid-range: 'data', 'end', or raise."""
    if header.get("ch") == LINK_CH:
        kind = header.get("type")
        if kind == "xfer_data":
            return "data"
        if kind == "xfer_end":
            return "end"
    raise TransferViolation(f"unexpected frame {json.dumps(header.get('type'))}")
