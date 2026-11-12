"""Files feature (`ch = "files"`): uses only the link's file transfer service."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

from clipbridge.link.feature import Sender
from clipbridge.link.peers import Peer
from clipbridge.link.transfer_core import FileOffer

log = logging.getLogger(__name__)

NAME = "files"


def default_dest_dir() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "Downloads" / "ClipShare"


def unique_path(directory: Path, name: str) -> Path:
    """`directory/name`, or `name (1)`, `name (2)`, ... before the extension if taken."""
    path = directory / name
    stem, suffix = path.stem, path.suffix
    n = 1
    while path.exists():
        path = directory / f"{stem} ({n}){suffix}"
        n += 1
    return path


class FilesFeature:
    name = NAME

    def __init__(
        self,
        link: Sender,
        dest_dir: Path | None = None,
        notify: Callable[[Path], None] | None = None,
    ) -> None:
        self._link = link
        self._dest_dir = dest_dir
        self._notify = notify

    async def send(self, path: Path) -> bool:
        """Send a file to the connected phone; True once it confirms the file."""
        peers = self._link.peers
        if not peers:
            log.info("no phone connected; not sending %s", path.name)
            return False
        return await self._link.send_file(peers[0], NAME, path)

    async def on_file_offer(self, peer: Peer, offer: FileOffer) -> Path | None:
        directory = self._dest_dir or default_dest_dir()
        return unique_path(directory, offer.name)

    async def on_file_received(self, peer: Peer, offer: FileOffer, path: Path) -> None:
        log.info("file received from %s: %s (%d bytes)", peer.device, path.name, offer.size)
        if self._notify is not None:
            self._notify(path)

    async def on_peer_connected(self, peer: Peer) -> None:
        return None

    async def on_peer_disconnected(self, peer: Peer) -> None:
        return None

    async def on_message(self, peer: Peer, header: dict[str, Any], body: bytes) -> None:
        return None  # files travel through the link's transfer service only
