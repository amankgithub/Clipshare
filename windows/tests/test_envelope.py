import json

import pytest

from clipbridge.link import envelope


def test_roundtrip_emoji_and_binary_body() -> None:
    header = {"ch": "x", "data": "Hi 😀👍🏽 नमस्ते 𝄞"}
    body = bytes(range(256))
    h, b = envelope.decode(envelope.encode(header, body))
    assert h == header and b == body


def test_accepts_escaped_surrogate_pairs() -> None:
    raw = b'{"ch":"x","d":"\\ud83d\\ude00"}'
    h, b = envelope.decode(len(raw).to_bytes(4, "big") + raw)
    assert h["d"] == "😀" and b == b""


@pytest.mark.parametrize(
    "plaintext",
    [
        b"\x00\x00",
        (99).to_bytes(4, "big") + b"{}",
        (2).to_bytes(4, "big") + b"\xff\xfe",
        (2).to_bytes(4, "big") + b"[]",
        (2).to_bytes(4, "big") + b"{}",
        len(b'{"ch":1}').to_bytes(4, "big") + b'{"ch":1}',
    ],
)
def test_malformed(plaintext: bytes) -> None:
    with pytest.raises(envelope.EnvelopeError):
        envelope.decode(plaintext)


def test_too_large() -> None:
    with pytest.raises(envelope.EnvelopeTooLarge):
        envelope.encode({"ch": "x"}, b"\x00" * envelope.MAX_PLAINTEXT)
    assert json.loads(envelope.encode({"ch": "x"})[4:]) == {"ch": "x"}
