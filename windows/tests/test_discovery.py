import asyncio

import pytest

from clipbridge.link import discovery


@pytest.mark.parametrize("addr", ["10.1.2.3", "172.16.0.1", "172.31.255.1", "192.168.1.5", "169.254.9.9"])
def test_private_addresses_advertised(addr, monkeypatch) -> None:
    monkeypatch.setattr(discovery, "get_all_addresses", lambda: [addr])
    assert discovery.local_ipv4() == [addr]


@pytest.mark.parametrize("addr", ["127.0.0.1", "8.8.8.8", "172.32.0.1", "100.64.0.1", "203.0.113.7"])
def test_other_addresses_not_advertised(addr, monkeypatch) -> None:
    monkeypatch.setattr(discovery, "get_all_addresses", lambda: [addr])
    assert discovery.local_ipv4() == []


# -- Advertiser with a fake Zeroconf ------------------------------------------------


class FakeZeroconf:
    fail_on_create = False
    instances: list["FakeZeroconf"] = []

    def __init__(self) -> None:
        if FakeZeroconf.fail_on_create:
            raise OSError("no multicast socket")
        self.registered: list[list[str]] = []
        self.unregistered = 0
        self.closed = False
        FakeZeroconf.instances.append(self)

    async def async_register_service(self, info) -> None:
        self.registered.append(info.parsed_addresses())

    async def async_unregister_service(self, info) -> None:
        self.unregistered += 1

    async def async_close(self) -> None:
        self.closed = True


@pytest.fixture
def fakezc(monkeypatch):
    FakeZeroconf.fail_on_create = False
    FakeZeroconf.instances = []
    monkeypatch.setattr(discovery, "AsyncZeroconf", FakeZeroconf)
    monkeypatch.setattr(discovery, "CHECK_INTERVAL", 0.05)
    return FakeZeroconf


async def wait_for(cond, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < deadline, "condition never became true"
        await asyncio.sleep(0.01)


async def test_advertiser_start_register_stop(fakezc, monkeypatch) -> None:
    monkeypatch.setattr(discovery, "local_ipv4", lambda: ["192.168.1.5"])
    adv = discovery.Advertiser(8765)
    await adv.start()
    (zc,) = fakezc.instances
    assert zc.registered == [["192.168.1.5"]]
    await adv.stop()
    assert zc.unregistered == 1 and zc.closed
    await adv.stop()  # idempotent
    assert zc.unregistered == 1


async def test_ip_change_reregisters(fakezc, monkeypatch) -> None:
    addrs = ["192.168.1.5"]
    monkeypatch.setattr(discovery, "local_ipv4", lambda: list(addrs))
    adv = discovery.Advertiser(8765)
    await adv.start()
    watcher = asyncio.create_task(adv.watch())
    try:
        (zc,) = fakezc.instances
        ticks = 0

        def counting() -> list[str]:
            nonlocal ticks
            ticks += 1
            return list(addrs)

        monkeypatch.setattr(discovery, "local_ipv4", counting)
        await wait_for(lambda: ticks >= 3)  # the watcher is polling
        assert zc.registered == [["192.168.1.5"]]  # unchanged address: no churn
        addrs[:] = ["10.0.0.7"]
        await wait_for(lambda: len(zc.registered) == 2)
        assert zc.registered[1] == ["10.0.0.7"] and zc.unregistered == 1
    finally:
        watcher.cancel()
        await adv.stop()


async def test_no_address_not_advertised_then_registers_when_one_appears(fakezc, monkeypatch) -> None:
    addrs: list[str] = []
    monkeypatch.setattr(discovery, "local_ipv4", lambda: list(addrs))
    adv = discovery.Advertiser(8765)
    await adv.start()
    (zc,) = fakezc.instances
    assert zc.registered == []
    watcher = asyncio.create_task(adv.watch())
    try:
        addrs.append("192.168.0.9")
        await wait_for(lambda: zc.registered == [["192.168.0.9"]])
    finally:
        watcher.cancel()
        await adv.stop()


async def test_request_refresh_reregisters_without_ip_change(fakezc, monkeypatch) -> None:
    monkeypatch.setattr(discovery, "local_ipv4", lambda: ["192.168.1.5"])
    monkeypatch.setattr(discovery, "CHECK_INTERVAL", 60.0)
    adv = discovery.Advertiser(8765)
    await adv.start()
    watcher = asyncio.create_task(adv.watch())
    try:
        (zc,) = fakezc.instances
        adv.request_refresh()
        await wait_for(lambda: len(zc.registered) == 2)
    finally:
        watcher.cancel()
        await adv.stop()


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="bug: AsyncZeroconf() failing in start() leaves _zc None, so every watch() tick's _register() "
    "hits `assert self._zc is not None` and mDNS is never advertised (app.py:101-103 promises a retry)",
)
async def test_zeroconf_create_failure_recovers_on_later_tick(fakezc, monkeypatch) -> None:
    monkeypatch.setattr(discovery, "local_ipv4", lambda: ["192.168.1.5"])
    fakezc.fail_on_create = True
    adv = discovery.Advertiser(8765)
    with pytest.raises(OSError):
        await adv.start()
    fakezc.fail_on_create = False
    watcher = asyncio.create_task(adv.watch())
    try:
        await asyncio.sleep(0.4)
        assert fakezc.instances and fakezc.instances[0].registered == [["192.168.1.5"]]
    finally:
        watcher.cancel()
        await adv.stop()
