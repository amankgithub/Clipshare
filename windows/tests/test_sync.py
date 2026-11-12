from clipbridge.features.clipboard.sync import Clip, SyncEngine


def t(s: str) -> Clip:
    return Clip("text", s)


def img(b: bytes) -> Clip:
    return Clip("image", b)


def test_local_send_and_echo_drop() -> None:
    e = SyncEngine()
    assert e.on_local(t("a"), connected=True) == t("a")
    assert e.on_local(t("a"), connected=True) is None  # same hash


def test_remote_then_local_echo_dropped() -> None:
    e = SyncEngine()
    assert e.on_remote(t("a")) is True
    assert e.on_local(t("a"), connected=True) is None  # our own write coming back
    assert e.on_remote(t("a")) is False


def test_recopy_after_other_text_allowed() -> None:
    e = SyncEngine()
    assert e.on_local(t("a"), True) == t("a")
    assert e.on_local(t("b"), True) == t("b")
    assert e.on_local(t("a"), True) == t("a")


def test_pending_single_slot_flushed_on_connect() -> None:
    e = SyncEngine()
    assert e.on_local(t("a"), connected=False) is None
    assert e.on_local(t("b"), connected=False) is None
    assert e.take_pending() == t("b")
    assert e.take_pending() is None


def test_remote_clears_pending() -> None:
    e = SyncEngine()
    e.on_local(t("a"), connected=False)
    assert e.on_remote(t("r")) is True
    assert e.take_pending() is None


def test_image_echo_drop() -> None:
    e = SyncEngine()
    assert e.on_remote(img(b"\x89PNG1")) is True
    assert e.on_local(img(b"\x89PNG1"), connected=True) is None
    assert e.on_remote(img(b"\x89PNG1")) is False
    assert e.on_local(img(b"\x89PNG2"), connected=True) == img(b"\x89PNG2")
    assert e.on_local(img(b"\x89PNG2"), connected=True) is None


def test_text_image_alternation() -> None:
    e = SyncEngine()
    assert e.on_local(t("a"), True) == t("a")
    assert e.on_local(img(b"x"), True) == img(b"x")
    assert e.on_local(t("a"), True) == t("a")  # shared slot: text after image is new
    assert e.on_remote(img(b"x")) is True
    assert e.on_remote(t("a")) is True


def test_image_pending_flushed() -> None:
    e = SyncEngine()
    assert e.on_local(img(b"x"), connected=False) is None
    assert e.take_pending() == img(b"x")
