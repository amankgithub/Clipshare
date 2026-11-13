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
