"""mDNS advertiser. Re-registers on IP change (30 s check) or on request (resume)."""

from __future__ import annotations

import asyncio
import logging
import re
import socket

from zeroconf import ServiceInfo, get_all_addresses
from zeroconf.asyncio import AsyncZeroconf

log = logging.getLogger(__name__)

SERVICE_TYPE = "_clipbridge._tcp.local."
CHECK_INTERVAL = 30.0


def local_ipv4() -> list[str]:
    """Usable IPv4 addresses of this machine (no loopback / link-local)."""
    addrs = [a for a in get_all_addresses() if not a.startswith(("127.", "169.254."))]
    return sorted(set(addrs))


class Advertiser:
    def __init__(self, port: int) -> None:
        self._port = port
        host = re.sub(r"[^A-Za-z0-9-]", "-", socket.gethostname()) or "windows"
        self._host = host
        self._zc: AsyncZeroconf | None = None
        self._info: ServiceInfo | None = None
        self._addrs: list[str] = []
        self._refresh = asyncio.Event()

    def _build(self, addrs: list[str]) -> ServiceInfo:
        return ServiceInfo(
            SERVICE_TYPE,
            f"ClipBridge-{self._host}.{SERVICE_TYPE}",
            port=self._port,
            parsed_addresses=addrs,
            server=f"{self._host}.local.",
        )

    async def start(self) -> None:
        self._zc = AsyncZeroconf()
        await self._register()

    async def _register(self) -> None:
        assert self._zc is not None
        if self._info is not None:
            await self._zc.async_unregister_service(self._info)
            self._info = None
        self._addrs = local_ipv4()
        if not self._addrs:
            log.warning("no usable IPv4 address; mDNS not advertised")
            return
        self._info = self._build(self._addrs)
        await self._zc.async_register_service(self._info)
        log.info("mDNS advertised on %s port %s", self._addrs, self._port)

    def request_refresh(self) -> None:
        """Call from the event loop thread (e.g. after system resume)."""
        self._refresh.set()

    async def watch(self) -> None:
        """Run forever; never raises."""
        while True:
            try:
                await asyncio.wait_for(self._refresh.wait(), CHECK_INTERVAL)
            except asyncio.TimeoutError:
                pass
            forced = self._refresh.is_set()
            self._refresh.clear()
            try:
                if forced or local_ipv4() != self._addrs:
                    log.info("re-registering mDNS (%s)", "resume" if forced else "IP change")
                    await self._register()
            except Exception:
                log.exception("mDNS refresh failed")

    async def stop(self) -> None:
        if self._zc is not None:
            try:
                if self._info is not None:
                    await self._zc.async_unregister_service(self._info)
                await self._zc.async_close()
            except Exception:
                log.exception("mDNS shutdown error")
            self._zc = None
