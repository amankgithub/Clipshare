"""Feature interface and the services the link gives each feature."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, Sequence

from clipbridge.link.peers import Peer
from clipbridge.link.requests import Answer, Reply
from clipbridge.link.transfer_core import FileOffer


class Feature(Protocol):
    name: str
    version: int  # highest version of this feature's messages it handles (sent in hello)

    async def on_peer_connected(self, peer: Peer) -> None: ...

    async def on_peer_disconnected(self, peer: Peer) -> None: ...

    async def on_message(self, peer: Peer, header: dict[str, Any], body: bytes) -> Answer | None:
        """Handle one message. The return value is the reply content if it was a request;
        raising makes the reply `ok: false`."""
        ...


class FileFeature(Feature, Protocol):
    """A feature that takes files (PROTOCOL.md section 6.3)."""

    async def on_file_offer(self, peer: Peer, offer: FileOffer) -> Path | None:
        """Final path for the incoming file (must not exist yet), or None to decline it."""
        ...

    async def on_file_received(self, peer: Peer, offer: FileOffer, path: Path) -> None: ...


class Sender(Protocol):
    """What a feature uses to talk to peers (implemented by the Link)."""

    @property
    def peers(self) -> Sequence[Peer]:
        """Connected peers whose hello has arrived."""
        ...

    async def send(self, peer: Peer, header: dict[str, Any], body: bytes = b"") -> bool: ...

    async def request(self, peer: Peer, header: dict[str, Any], body: bytes = b"") -> Reply | None:
        """Send as a request; the peer's reply, or None if none came (timeout, closed, send failed)."""
        ...

    async def broadcast(
        self, header: dict[str, Any], body: bytes = b"", exclude: Peer | None = None
    ) -> None:
        """Queue for every peer whose hello lists `header["ch"]`, except `exclude`.

        Does not wait for delivery or for slow peers.
        """
        ...

    async def send_file(
        self, peer: Peer, feature: str, path: Path, meta: dict[str, Any] | None = None
    ) -> bool:
        """Queue a file for the peer's `feature`; True once the peer confirms it, False on failure.

        Waits across reconnects (the file is re-offered and resumes).
        """
        ...
