"""Feature interface and the services the link gives each feature."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, Sequence

from clipbridge.link.peers import Peer
from clipbridge.link.transfer_core import FileOffer


class Feature(Protocol):
    name: str

    async def on_peer_connected(self, peer: Peer) -> None: ...

    async def on_peer_disconnected(self, peer: Peer) -> None: ...

    async def on_message(self, peer: Peer, header: dict[str, Any], body: bytes) -> None: ...


class FileFeature(Feature, Protocol):
    """A feature that takes files (PROTOCOL.md section 6.2)."""

    async def on_file_offer(self, peer: Peer, offer: FileOffer) -> Path | None:
        """Final path for the incoming file (must not exist yet), or None to decline it."""
        ...

    async def on_file_received(self, peer: Peer, offer: FileOffer, path: Path) -> None: ...


class Sender(Protocol):
    """What a feature uses to talk to peers (implemented by the Link)."""

    @property
    def peers(self) -> Sequence[Peer]: ...

    async def send(self, peer: Peer, header: dict[str, Any], body: bytes = b"") -> bool: ...

    async def broadcast(
        self, header: dict[str, Any], body: bytes = b"", exclude: Peer | None = None
    ) -> None:
        """Queue for every peer except `exclude`; does not wait for delivery or for slow peers."""
        ...

    async def send_file(
        self, peer: Peer, feature: str, path: Path, meta: dict[str, Any] | None = None
    ) -> bool:
        """Queue a file for the peer's `feature`; True once the peer confirms it, False on failure.

        Waits across reconnects (the file is re-offered and resumes).
        """
        ...
