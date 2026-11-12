"""Generate protocol/test_vectors.json from the reference crypto in PROTOCOL.md.

Deterministic: fixed inputs, no randomness. Re-running must produce a
byte-identical file. Requires the `cryptography` package.

    python protocol/gen_vectors.py
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

OUT = Path(__file__).with_name("test_vectors.json")

PSK = bytes(range(0x00, 0x20))  # 32 bytes
SN = bytes(range(0x40, 0x50))  # 16 bytes, server nonce
CN = bytes(range(0x80, 0x90))  # 16 bytes, client nonce
DEVICE = "android-test-01"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def client_mac(psk: bytes, sn: bytes, cn: bytes, device: str) -> bytes:
    return hmac.new(psk, b"c" + sn + cn + device.encode("utf-8"), hashlib.sha256).digest()


def server_mac(psk: bytes, sn: bytes, cn: bytes) -> bytes:
    return hmac.new(psk, b"s" + sn + cn, hashlib.sha256).digest()


def derive_key(psk: bytes, sn: bytes, cn: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=sn + cn, info=info).derive(psk)


def nonce(counter: int) -> bytes:
    return struct.pack("<Q", counter) + b"\x00" * 4


def envelope(header: dict, body: bytes) -> tuple[bytes, bytes]:
    header_bytes = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return header_bytes, struct.pack(">I", len(header_bytes)) + header_bytes + body


def frame(key: bytes, direction: str, counter: int, header: dict, body: bytes) -> dict:
    header_bytes, plaintext = envelope(header, body)
    n = nonce(counter)
    sealed = AESGCM(key).encrypt(n, plaintext, None)
    return {
        "direction": direction,
        "counter": counter,
        "nonce": n.hex(),
        "header": header,
        "header_utf8": header_bytes.hex(),
        "body": body.hex(),
        "plaintext": plaintext.hex(),
        "frame": (n + sealed).hex(),
    }


def main() -> None:
    c2s = derive_key(PSK, SN, CN, b"c2s")
    s2c = derive_key(PSK, SN, CN, b"s2c")
    cmac = client_mac(PSK, SN, CN, DEVICE)
    smac = server_mac(PSK, SN, CN)

    png_like = bytes.fromhex("89504e470d0a1a0a") + bytes(range(256))
    frames = [
        frame(s2c, "s2c", 0,
              {"ch": "clipboard", "type": "clip", "kind": "text", "data": "hello", "ts": 1700000000000},
              b""),
        frame(c2s, "c2s", 1,
              {"ch": "clipboard", "type": "clip", "kind": "text",
               "data": "Hi \U0001F600\U0001F44D\U0001F3FD नमस्ते \U0001D11E \"q\"\n",
               "ts": 1700000000001},
              b""),
        frame(s2c, "s2c", 0x00000001_00000005,
              {"ch": "clipboard", "type": "clip", "kind": "image", "mime": "image/png", "ts": 1700000000002},
              png_like),
    ]

    vectors = {
        "version": 1,
        "description": "ClipBridge v1 interop vectors. All byte strings are lowercase hex. See PROTOCOL.md.",
        "psk": PSK.hex(),
        "psk_b64": b64(PSK),
        "sn": SN.hex(),
        "sn_b64": b64(SN),
        "cn": CN.hex(),
        "cn_b64": b64(CN),
        "device": DEVICE,
        "client_mac_input": (b"c" + SN + CN + DEVICE.encode("utf-8")).hex(),
        "client_mac": cmac.hex(),
        "client_mac_b64": b64(cmac),
        "server_mac_input": (b"s" + SN + CN).hex(),
        "server_mac": smac.hex(),
        "server_mac_b64": b64(smac),
        "key_c2s": c2s.hex(),
        "key_s2c": s2c.hex(),
        "frames": frames,
    }
    text = json.dumps(vectors, indent=2, ensure_ascii=True) + "\n"
    OUT.write_bytes(text.encode("ascii"))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
