import json
from pathlib import Path

import pytest

from clipbridge.link import crypto, envelope

VECTORS = json.loads((Path(__file__).parents[2] / "protocol" / "test_vectors.json").read_text("utf-8"))
H = bytes.fromhex
PSK, SN, CN = H(VECTORS["psk"]), H(VECTORS["sn"]), H(VECTORS["cn"])


def test_macs() -> None:
    assert crypto.client_mac(PSK, SN, CN, VECTORS["device"]) == H(VECTORS["client_mac"])
    assert crypto.server_mac(PSK, SN, CN) == H(VECTORS["server_mac"])


def test_keys() -> None:
    c2s, s2c = crypto.derive_keys(PSK, SN, CN)
    assert c2s == H(VECTORS["key_c2s"])
    assert s2c == H(VECTORS["key_s2c"])


@pytest.mark.parametrize("frame", VECTORS["frames"], ids=lambda f: f"{f['direction']}-{f['counter']}")
def test_frames(frame: dict) -> None:
    key = H(VECTORS["key_" + frame["direction"]])
    plaintext, wire = H(frame["plaintext"]), H(frame["frame"])
    assert crypto.seal(key, frame["counter"], plaintext) == wire
    assert crypto.open_frame(key, wire, frame["counter"]) == plaintext
    header, body = envelope.decode(plaintext)
    assert header == frame["header"]
    assert body == H(frame["body"])


def test_frame_checks() -> None:
    key = H(VECTORS["key_c2s"])
    good = crypto.seal(key, 0, b"x")
    with pytest.raises(crypto.FrameError):
        crypto.open_frame(key, good[:20], 0)  # short
    with pytest.raises(crypto.FrameError):
        crypto.open_frame(key, good, 1)  # counter
    with pytest.raises(crypto.FrameError):
        crypto.open_frame(key, good[:8] + b"\x01" + good[9:], 0)  # nonce padding
    with pytest.raises(crypto.FrameError):
        crypto.open_frame(key, good[:-1] + bytes([good[-1] ^ 1]), 0)  # tag


def test_channel_counters() -> None:
    a = crypto.SecureChannel(b"k" * 32, b"j" * 32)
    b = crypto.SecureChannel(b"j" * 32, b"k" * 32)
    f0, f1 = a.encrypt(b"one"), a.encrypt(b"two")
    assert b.decrypt(f0) == b"one" and b.decrypt(f1) == b"two"
    with pytest.raises(crypto.FrameError):
        b.decrypt(f0)  # replay
