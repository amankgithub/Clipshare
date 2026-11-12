import hashlib

import pytest

from clipbridge.link import transfer_core as core
from clipbridge.link.transfer_core import Range, RangeStream, TransferViolation

FID = "0123456789abcdef0123456789abcdef"


def offer(**kw: object) -> dict:
    return {"ch": "link", "type": "xfer_offer", "id": FID, "feature": "files", "name": "a.txt", "size": 5, "meta": {}, **kw}


def test_split() -> None:
    assert core.split(0) == []
    assert core.split(10, 1) == [Range(0, 10)]
    assert core.split(10, 3) == [Range(0, 4), Range(4, 7), Range(7, 10)]
    assert core.split(2, 5) == [Range(0, 1), Range(1, 2)]


def test_parse_ranges() -> None:
    assert core.parse_ranges([], 0) == []
    assert core.parse_ranges([[0, 4, 2], [4, 10, 0]], 10) == [Range(0, 4, 2), Range(4, 10, 0)]
    for bad in ([[0, 4, 0]], [[1, 10, 0]], [[0, 10, 11]], [[0, 5, 0], [4, 10, 0]], [[0, 10]], [[0, 10, True]], "x"):
        assert core.parse_ranges(bad, 10) is None


def test_parse_offer() -> None:
    o = core.parse_offer(offer(name="..\\..\\evil:name?.txt"))
    assert o is not None and o.name == "evil_name_.txt" and o.size == 5
    assert core.parse_offer(offer(id="ABC")) is None
    assert core.parse_offer(offer(size=-1)) is None
    assert core.parse_offer(offer(size=True)) is None
    assert core.parse_offer(offer(name="")) is None
    assert core.parse_offer(offer(meta=[])) is None
    assert core.parse_offer(offer(feature="")) is None


@pytest.mark.parametrize(
    "raw,safe",
    [("a.txt", "a.txt"), ("dir/b.pdf", "b.pdf"), ("CON.txt", "_CON.txt"), ("..", "file"),
     ("x. ", "x"), ("tab\there", "tab_here"), ("😀.png", "😀.png")],
)
def test_safe_name(raw: str, safe: str) -> None:
    assert core.safe_name(raw) == safe


def test_safe_name_truncates_keeping_extension() -> None:
    name = core.safe_name("a" * 300 + ".jpeg")
    assert len(name) == 255 and name.endswith(".jpeg")


def test_range_stream_resume_hash() -> None:
    data = bytes(range(256)) * 4
    s = RangeStream(Range(0, len(data), 100))
    s.prime(data[:100])
    assert s.position == 100
    s.feed(data[100:600])
    s.feed(data[600:])
    assert s.range.have == len(data)
    assert s.finish(core.end(hashlib.sha256(data).hexdigest()))


def test_range_stream_mismatch_and_violations() -> None:
    s = RangeStream(Range(10, 20))
    with pytest.raises(TransferViolation):
        s.finish(core.end("0" * 64))  # end before end of range
    with pytest.raises(TransferViolation):
        s.feed(b"x" * 11)  # past end
    with pytest.raises(TransferViolation):
        s.feed(b"")  # empty chunk
    s.feed(b"x" * 10)
    assert s.finish(core.end("0" * 64)) is False
    with pytest.raises(TransferViolation):
        s.finish(core.end("nothex"))


def test_classify_and_attach() -> None:
    assert core.classify(core.data()) == "data"
    assert core.classify(core.end("0" * 64)) == "end"
    with pytest.raises(TransferViolation):
        core.classify({"ch": "clipboard", "type": "clip"})
    assert core.parse_attach(core.attach(FID, 0)) == (FID, 0)
    assert core.parse_attach({"ch": "link", "type": "xfer_attach", "id": FID}) is None
