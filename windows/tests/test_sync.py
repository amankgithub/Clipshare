from clipbridge.features.clipboard.sync import SyncEngine


def test_local_send_and_echo_drop() -> None:
    e = SyncEngine()
    assert e.on_local("a", connected=True) == "a"
    assert e.on_local("a", connected=True) is None  # same hash


def test_remote_then_local_echo_dropped() -> None:
    e = SyncEngine()
    assert e.on_remote("a") is True
    assert e.on_local("a", connected=True) is None  # our own write coming back
    assert e.on_remote("a") is False


def test_recopy_after_other_text_allowed() -> None:
    e = SyncEngine()
    assert e.on_local("a", True) == "a"
    assert e.on_local("b", True) == "b"
    assert e.on_local("a", True) == "a"


def test_pending_single_slot_flushed_on_connect() -> None:
    e = SyncEngine()
    assert e.on_local("a", connected=False) is None
    assert e.on_local("b", connected=False) is None
    assert e.take_pending() == "b"
    assert e.take_pending() is None


def test_remote_clears_pending() -> None:
    e = SyncEngine()
    e.on_local("a", connected=False)
    assert e.on_remote("r") is True
    assert e.take_pending() is None
