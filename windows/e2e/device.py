"""Device scenario runner (PLAN.md step 13): real PC server + real app on the phone over adb.

    windows\\.venv\\Scripts\\python windows\\e2e\\device.py [--quick] [--only 1,2,8]

Stdlib only. Never prints clipboard contents (lengths/hashes only). Cleans up every file it creates.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

MB = 1024 * 1024
WINDOWS_DIR = Path(__file__).resolve().parents[1]
PC_DIR = Path(os.environ["USERPROFILE"]) / "Downloads" / "ClipShare"
PARTIAL_DIR = Path(os.environ["LOCALAPPDATA"]) / "ClipBridge" / "partial"
WIN_LOG = Path(os.environ["LOCALAPPDATA"]) / "ClipBridge" / "logs" / "clipbridge.log"
PHONE_DIR = "/sdcard/Download/ClipShare"
PHONE_E2E = "/sdcard/Android/data/com.clipbridge/files/e2e"
PKG = "com.clipbridge"
CLIP_LIMIT_S = 1.0
RECONNECT_LIMIT_S = 15.0  # 10 s backoff cap + dial
DEAD_HOST = "10.255.255.1"
MANUAL_DEAD_LIMIT_S = 18.0  # from Wi-Fi up: 4 s connect timeout + up to 10 s handshake timer + mDNS connect + margin
PAIR_ACTIVITY = f"{PKG}/.ui.PairActivity"


class Skip(Exception):
    pass


class Fail(Exception):
    pass


class Recovery(Exception):
    """The phone could not be restored to its real pairing."""


class Xfail(Exception):
    """The known-bug symptom was observed (expected failure)."""


SECRETS: list[str] = []  # PSKs seen by this run; scrubbed from anything printed


def scrub(text: str) -> str:
    for secret in SECRETS:
        text = text.replace(secret, "<redacted>")
    return text


# ---------------------------------------------------------------- adb / shell

def run(cmd: list[str], timeout: float = 120, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env,
                          encoding="utf-8", errors="replace")


def adb(*args: str, timeout: float = 120) -> str:
    r = run(["adb", *args], timeout=timeout)
    return (r.stdout or "") + (r.stderr or "")


def sh(cmd: str, timeout: float = 120) -> str:
    return adb("shell", cmd, timeout=timeout)


# ---------------------------------------------------------------- Windows clipboard (ctypes)

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_u32.OpenClipboard.argtypes = [wintypes.HWND]
_u32.OpenClipboard.restype = wintypes.BOOL
_u32.CloseClipboard.restype = wintypes.BOOL
_u32.EmptyClipboard.restype = wintypes.BOOL
_u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_u32.SetClipboardData.restype = wintypes.HANDLE
_u32.GetClipboardData.argtypes = [wintypes.UINT]
_u32.GetClipboardData.restype = wintypes.HANDLE
_k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_k32.GlobalAlloc.restype = wintypes.HANDLE
_k32.GlobalLock.argtypes = [wintypes.HANDLE]
_k32.GlobalLock.restype = ctypes.c_void_p
_k32.GlobalUnlock.argtypes = [wintypes.HANDLE]
CF_UNICODETEXT = 13


def _open_clipboard() -> None:
    for _ in range(40):
        if _u32.OpenClipboard(None):
            return
        time.sleep(0.05)
    raise Fail("cannot open Windows clipboard")


def clip_set(text: str) -> None:
    data = (text + "\0").encode("utf-16-le")
    _open_clipboard()
    try:
        _u32.EmptyClipboard()
        h = _k32.GlobalAlloc(0x0002, len(data))
        p = _k32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        _k32.GlobalUnlock(h)
        _u32.SetClipboardData(CF_UNICODETEXT, h)
    finally:
        _u32.CloseClipboard()


def clip_get() -> str | None:
    try:
        _open_clipboard()
    except Fail:
        return None
    try:
        h = _u32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = _k32.GlobalLock(h)
        try:
            return ctypes.wstring_at(p)
        finally:
            _k32.GlobalUnlock(h)
    finally:
        _u32.CloseClipboard()


def java_hash(text: str) -> int:
    units = text.encode("utf-16-le")
    h = 0
    for i in range(0, len(units), 2):
        h = (31 * h + int.from_bytes(units[i:i + 2], "little")) & 0xFFFFFFFF
    return h - (1 << 32) if h >= (1 << 31) else h


# ---------------------------------------------------------------- phone log tail

class LogTail:
    """Streams `adb logcat -s ClipBridge:I`; keeps (monotonic time, line)."""

    def __init__(self) -> None:
        adb("logcat", "-c")
        self.lines: list[tuple[float, str]] = []
        self._p = subprocess.Popen(["adb", "logcat", "-v", "brief", "-s", "ClipBridge:I"], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self._p.stdout
        for line in self._p.stdout:
            self.lines.append((time.monotonic(), line.rstrip()))

    def mark(self) -> int:
        return len(self.lines)

    def since(self, mark: int) -> list[str]:
        return [t for _, t in self.lines[mark:]]

    def wait(self, pattern: str, mark: int, timeout: float) -> tuple[float, re.Match] | None:
        rx = re.compile(pattern)
        end = time.monotonic() + timeout
        seen = mark
        while True:
            while seen < len(self.lines):
                t, line = self.lines[seen]
                seen += 1
                m = rx.search(line)
                if m:
                    return t, m
            if time.monotonic() > end:
                return None
            time.sleep(0.02)

    def close(self) -> None:
        self._p.terminate()


# ---------------------------------------------------------------- test file generation

def make_file(path: Path, size: int) -> None:
    with open(path, "wb") as f:
        left = size
        while left > 0:
            n = min(left, 4 * MB)
            f.write(os.urandom(n))
            left -= n


def make_jpeg(path: Path, size: int) -> None:
    head = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    body = bytearray()
    room = size - len(head) - 2
    while room > 0:
        n = min(room - 4, 65000)
        if n <= 0:
            break
        body += b"\xff\xfe" + (n + 2).to_bytes(2, "big") + os.urandom(n)
        room -= n + 4
    path.write_bytes(head + bytes(body) + b"\xff\xd9")


def make_pdf(path: Path, size: int) -> None:
    head = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n")
    tail = b"\ntrailer<</Root 1 0 R/Size 4>>\n%%EOF\n"
    pad = os.urandom(max(0, size - len(head) - len(tail)))
    path.write_bytes(head + b"%" + pad.replace(b"\n", b"\x01").replace(b"\r", b"\x01") + tail)


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(4 * MB):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- context

@dataclass
class Ctx:
    quick: bool
    rid: str
    tmp: Path
    log: LogTail
    hook: bool = False
    winlog_ok: bool = False
    src: dict[str, tuple[Path, str]] = field(default_factory=dict)  # name -> (path, sha)
    on_phone: set[str] = field(default_factory=set)  # names pushed to the e2e dir
    partial_names: set[str] = field(default_factory=set)  # partial-dir entries that existed before this run
    orig_host: str | None = None  # the phone's saved manual host before --pairing scenarios
    ready: bool = False  # preconditions passed
    clips: tuple[int, int, int] | None = None  # scenario 8: hashes (pc->phone, phone->pc) and log mark
    clip_mark: int = 0
    win_mark: int = 0
    real: tuple[str, str] | None = None  # (device id, PSK b64) for --pairing; never printed

    @property
    def big(self) -> int:
        return 128 * MB if self.quick else 1024 * MB

    @property
    def mix_big(self) -> int:
        return 32 * MB if self.quick else 200 * MB

    @property
    def photo(self) -> int:
        return 1 * MB if self.quick else 4 * MB

    def name(self, tag: str, ext: str = "bin") -> str:
        return f"e2e{self.rid}_{tag}.{ext}"

    def gen(self, tag: str, size: int, kind: str = "bin") -> str:
        ext = {"bin": "bin", "jpg": "jpg", "pdf": "pdf"}[kind]
        name = self.name(tag, ext)
        if name not in self.src:
            path = self.tmp / name
            {"bin": make_file, "jpg": make_jpeg, "pdf": make_pdf}[kind](path, size)
            self.src[name] = (path, sha_file(path))
        return name

    def variant(self, name: str, size: int) -> str:
        """Second source file with the same basename but different content; returns its key for path()/sha()."""
        key = name + "#2"
        if key not in self.src:
            d = self.tmp / "v2"
            d.mkdir(exist_ok=True)
            path = d / name
            make_file(path, size)
            self.src[key] = (path, sha_file(path))
        return key

    def path(self, name: str) -> Path:
        return self.src[name][0]

    def sha(self, name: str) -> str:
        return self.src[name][1]

    def size(self, name: str) -> int:
        return self.path(name).stat().st_size


def stem_ext(name: str) -> tuple[str, str]:
    stem, dot, ext = name.rpartition(".")
    return stem, dot + ext


# ---------------------------------------------------------------- PC <-> phone operations

def pc_send(ctx: Ctx, names: list[str]) -> None:
    env = dict(os.environ, PYTHONPATH=str(WINDOWS_DIR / "src"))
    r = run([sys.executable, "-m", "clipbridge", "send", *[str(ctx.path(n)) for n in names]], cwd=WINDOWS_DIR, env=env)
    if r.returncode != 0:
        raise Fail(f"clipbridge send failed: {(r.stdout + r.stderr).strip()[:160]}")


def phone_stat(prefix: str) -> dict[str, int]:
    out = sh(f"stat -c '%n %s' {PHONE_DIR}/{prefix}* 2>/dev/null")
    res: dict[str, int] = {}
    for line in out.splitlines():
        m = re.match(r"(.+) (\d+)$", line.strip())
        if m:
            res[m.group(1).rsplit("/", 1)[-1]] = int(m.group(2))
    return res


def phone_pending(ctx: Ctx) -> dict[str, int]:
    out = sh(f"stat -c '%n %s' {PHONE_DIR}/.pending*e2e{ctx.rid}_* 2>/dev/null")
    res: dict[str, int] = {}
    for line in out.splitlines():
        m = re.match(r"(.+) (\d+)$", line.strip())
        if m:
            res[m.group(1).rsplit("/", 1)[-1]] = int(m.group(2))
    return res


def phone_progress(ctx: Ctx, name: str) -> int | None:
    """Bytes of an in-flight incoming file on the phone (its pending file), or None if there is none."""
    for n, size in phone_pending(ctx).items():
        if n.endswith(name):
            return size
    return None


def ensure_moving_phone(ctx: Ctx, name: str, total: int, what: str = "transfer") -> int:
    """Prove a PC->phone transfer is in flight: pending size is below total and grows between two samples."""
    end = time.monotonic() + 30
    while time.monotonic() < end:
        a = phone_progress(ctx, name)
        if a is not None and 0 < a < total:
            time.sleep(0.4)
            b = phone_progress(ctx, name)
            if b is not None and a < b < total:
                return b
            if b is None:
                break
        elif a is None and phone_stat(f"e2e{ctx.rid}_").get(name) == total:
            break
        else:
            time.sleep(0.2)
    raise Fail(f"{what} not in flight (finished early or stalled); use a bigger file")


def pc_have(ctx: Ctx, name: str) -> int:
    """Bytes received so far of the in-flight incoming transfer whose offer name is `name` (its `<id>.json`)."""
    for p in PARTIAL_DIR.glob("*.json") if PARTIAL_DIR.exists() else []:
        if p.name == "completed.json":
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("offer", {}).get("name") == name:
            return sum(int(r[2]) for r in data.get("ranges", []))  # ranges are [start, length, have]
    return 0


def ensure_moving_pc(ctx: Ctx, name: str, total: int, what: str = "transfer") -> int:
    """Prove a phone->PC transfer is in flight: not finished at the destination and `have` grows."""
    end = time.monotonic() + 30
    while time.monotonic() < end:
        a = pc_have(ctx, name)
        if 0 < a < total and not (PC_DIR / name).exists():
            time.sleep(0.5)
            b = pc_have(ctx, name)
            if a < b < total and not (PC_DIR / name).exists():
                return b
        time.sleep(0.2)
    raise Fail(f"{what} not in flight on the PC (finished early or no progress visible); use a bigger file")


def phone_sha(name: str) -> str:
    out = sh(f"sha256sum '{PHONE_DIR}/{name}'", timeout=300)
    return out.split()[0] if out.strip() else ""


def timeout_for(size: int) -> float:
    return 90 + size / (1.5 * MB)


def wait_phone(ctx: Ctx, expect: dict[str, int], timeout: float) -> None:
    """Wait until every expected name exists on the phone with its size (pending files do not match the prefix)."""
    end = time.monotonic() + timeout
    got: dict[str, int] = {}
    while time.monotonic() < end:
        got = phone_stat(f"e2e{ctx.rid}_")
        if all(got.get(n) == s for n, s in expect.items()):
            return
        time.sleep(0.5)
    missing = [n for n, s in expect.items() if got.get(n) != s]
    raise Fail(f"phone never got {missing[:3]} (timeout {timeout:.0f}s)")


def verify_phone(ctx: Ctx, pairs: dict[str, str]) -> None:
    """pairs: phone name -> source name. Check SHA-256 and that nothing is left pending."""
    for pn, sn in pairs.items():
        if phone_sha(pn) != ctx.sha(sn):
            raise Fail(f"hash mismatch on phone: {pn}")
    if phone_pending(ctx):
        raise Fail("pending file left on phone")


def hook_share(ctx: Ctx, names: list[str]) -> None:
    if not ctx.hook:
        raise Skip("hook missing")
    for n in names:
        if n not in ctx.on_phone:
            r = adb("push", str(ctx.path(n)), f"{PHONE_E2E}/{n}", timeout=timeout_for(ctx.size(n)))
            if "pushed" not in r:
                raise Fail(f"adb push failed: {r.strip()[:120]}")
            ctx.on_phone.add(n)
    mark = ctx.log.mark()
    adb("shell", "am", "start", "-n", f"{PKG}/.debug.E2eShareActivity", "--esa", "names", ",".join(names))
    hit = ctx.log.wait(r"e2e share (n=\d+|missing)", mark, 8)
    if not hit or hit[1].group(1) != f"n={len(names)}":
        raise Fail("hook did not share: " + (hit[1].group(1) if hit else "no log line"))


def wait_pc(ctx: Ctx, expect: dict[str, int], timeout: float) -> float:
    """expect: PC name -> size. Returns the time the last one was seen."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if all((PC_DIR / n).is_file() and (PC_DIR / n).stat().st_size == s for n, s in expect.items()):
            return time.monotonic()
        time.sleep(0.2)
    missing = [n for n, s in expect.items() if not (PC_DIR / n).is_file() or (PC_DIR / n).stat().st_size != s]
    raise Fail(f"PC never got {missing[:3]} (timeout {timeout:.0f}s)")


def verify_pc(ctx: Ctx, pairs: dict[str, str]) -> None:
    for pn, sn in pairs.items():
        if sha_file(PC_DIR / pn) != ctx.sha(sn):
            raise Fail(f"hash mismatch on PC: {pn}")
    end = time.monotonic() + 5
    while True:
        left = leftover_partials(ctx)
        if not left or time.monotonic() > end:
            break
        time.sleep(0.3)
    if left:
        raise Fail(f"{len(left)} partial file(s) from this run left in {PARTIAL_DIR}")


def leftover_partials(ctx: Ctx) -> list[str]:
    """Partial-dir entries that appeared during this run (completed.json is the server's own record and is ignored)."""
    if not PARTIAL_DIR.exists():
        return []
    return [p.name for p in PARTIAL_DIR.glob("*") if p.name != "completed.json" and p.name not in ctx.partial_names]


def clean_dests(ctx: Ctx) -> None:
    pre = f"e2e{ctx.rid}_"
    if PC_DIR.exists():
        for p in PC_DIR.glob(pre + "*"):
            try:
                p.unlink()
            except OSError:
                pass
    sh(f"rm -f {PHONE_DIR}/{pre}* {PHONE_DIR}/.pending*{pre}* 2>/dev/null")


def winlog_pos() -> int:
    try:
        return WIN_LOG.stat().st_size
    except OSError:
        return 0


def winlog_since(pos: int) -> list[str]:
    try:
        with open(WIN_LOG, "rb") as f:
            f.seek(pos)
            return f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def new_clip(ctx: Ctx, tag: str) -> str:
    return f"e2e-{ctx.rid}-{tag}-" + "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=24))


def pc_clip_to_phone(ctx: Ctx, text: str) -> float:
    mark = ctx.log.mark()
    t0 = time.monotonic()
    clip_set(text)
    h = java_hash(text)
    hit = ctx.log.wait(rf"remote clip len={len(text)} hash={h} applied=true", mark, 8)
    if not hit:
        raise Fail(f"phone did not apply PC clip (len={len(text)} hash={h})")
    return hit[0] - t0


def phone_clip_to_pc(ctx: Ctx, text: str) -> float:
    """Latency from the phone's `send clip` log line to the text appearing in the Windows clipboard."""
    clip_set(f"e2e-sentinel-{ctx.rid}")  # non-empty stale value that cannot match
    mark = ctx.log.mark()
    t0 = time.monotonic()
    adb("shell", "am", "start", "-n", f"{PKG}/.features.clipboard.ShareTargetActivity", "-a",
        "android.intent.action.SEND", "-t", "text/plain", "--es", "android.intent.extra.TEXT", text)
    end = t0 + 8
    while time.monotonic() < end:
        if clip_get() == text:
            t_pc = time.monotonic()
            hit = ctx.log.wait(rf"send clip len={len(text)} hash={java_hash(text)}\b", mark, 2)
            if not hit:
                raise Fail("phone log has no 'send clip' line for the shared text")
            return max(0.0, t_pc - hit[0])
        time.sleep(0.03)
    raise Fail(f"PC clipboard did not get phone clip (len={len(text)} hash={java_hash(text)})")



# ---------------------------------------------------------------- link state, PC task, Wi-Fi, pairing

def phone_state() -> tuple[str, str]:
    """(kind, text). kind: connected | disconnected (app alive) | dead (app process gone) | unknown (adb/dumpsys error).

    text is the foreground notification ('Connected to ...', 'Searching for PC (...)').
    """
    out = adb("shell", "dumpsys notification --noredact", timeout=30)
    if "Notification" not in out:
        return "unknown", "dumpsys failed"
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if "NotificationRecord" in line and "pkg=com.clipbridge" in line and " id=1 " in line:
            for nxt in lines[i:i + 120]:
                m = re.search(r"android\.text=String \((.*)\)\s*$", nxt)
                if m:
                    text = m.group(1)
                    return ("connected" if text.startswith("Connected") else "disconnected"), text
            return "unknown", "notification without text"
    pid = sh(f"pidof {PKG}").strip()
    if not re.fullmatch(r"[0-9 ]*", pid):
        return "unknown", "pidof failed"
    return ("disconnected", "no notification") if pid else ("dead", "app process not running")


def phone_status() -> str:
    return phone_state()[1]


def phone_manual_host() -> str | None:
    """The app's saved manual host (only the value of the `manual` key is extracted; the prefs file also holds the PSK)."""
    xml = adb("shell", f"run-as {PKG} cat shared_prefs/clipbridge.xml")
    m = re.search(r'<string name="manual">([^<]*)</string>', xml)
    if m:
        return m.group(1)
    return "" if "<map" in xml else None


def wait_connected(timeout: float, t0: float | None = None, seen: list[str] | None = None) -> float:
    """Seconds from t0 (default: now) until the phone reports Connected; seen collects the statuses on the way."""
    t0 = time.monotonic() if t0 is None else t0
    end = time.monotonic() + timeout
    last = ("unknown", "")
    while time.monotonic() < end:
        last = phone_state()
        if seen is not None:
            seen.append(last[1])
        if last[0] == "connected":
            return time.monotonic() - t0
        time.sleep(0.2)
    raise Fail(f"phone not connected after {timeout:.0f}s (state: {last[0]}, status: {last[1]})")


def wait_not_connected(timeout: float, why: str, allow_dead: bool = False) -> str:
    """Wait for a real disconnect: the app is alive and not Connected. adb/dumpsys errors never count."""
    end = time.monotonic() + timeout
    last = ("unknown", "")
    while time.monotonic() < end:
        last = phone_state()
        if last[0] == "disconnected":
            return last[1]
        if last[0] == "dead":
            if allow_dead:
                return last[1]
            raise Fail("the app process died instead of just disconnecting")
        time.sleep(0.2)
    raise Fail(why + (" (adb/dumpsys unreliable)" if last[0] == "unknown" else ""))


def ps(cmd: str) -> str:
    return run(["powershell", "-NoProfile", "-Command", cmd], timeout=60).stdout.strip()


def ensure_task_running(wait: float = 0) -> None:
    end = time.monotonic() + wait
    while True:
        if ps("(Get-ScheduledTask -TaskName ClipBridge).State") == "Running":
            return
        ps("Start-ScheduledTask -TaskName ClipBridge")
        if time.monotonic() >= end:
            return
        time.sleep(2)


def wifi_off() -> None:
    sh("svc wifi disable")
    wait_not_connected(20, "phone still reports Connected 20 s after Wi-Fi off")


def wifi_on() -> float:
    """Enable Wi-Fi; returns the monotonic time it was enabled."""
    t = time.monotonic()
    sh("svc wifi enable")
    return t


def pair_phone(device: str | None = None, psk_b64: str | None = None, host: str | None = None) -> None:
    """Same adb call as `clipbridge pair`. Values go only on the command line; errors never carry argv."""
    cmd = ["adb", "shell", "am", "start", "-n", PAIR_ACTIVITY]
    for key, val in (("device", device), ("psk", psk_b64), ("host", host)):
        if val is not None:
            cmd += ["--es", key, val if val != "" else "''"]  # adb joins args, so an empty value needs explicit quotes
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 - TimeoutExpired/OSError text contains argv (the PSK)
        raise Fail(f"PairActivity call failed ({type(e).__name__})") from None
    if r.returncode != 0 or "Error" in (r.stdout or ""):
        raise Fail("PairActivity call failed")


def real_pairing() -> tuple[str, str]:
    sys.path.insert(0, str(WINDOWS_DIR / "src"))
    from clipbridge import config  # noqa: PLC0415 - needs pywin32, only for --pairing

    paired = config.DeviceRegistry().load()
    if len(paired) != 1:
        raise SystemExit(f"PRECONDITION FAILED: --pairing needs exactly 1 paired device, found {len(paired)}")
    device, entry = next(iter(paired.items()))
    psk = base64.b64encode(entry.psk).decode("ascii")
    SECRETS.append(psk)
    return device, psk


def restore_pairing(ctx: Ctx) -> None:
    """Put the real device id, PSK and the ORIGINAL manual host back; prove the link and a clip work; else Recovery."""
    try:
        device, psk = ctx.real  # type: ignore[misc]
        pair_phone(device, psk, ctx.orig_host or "")
        if phone_manual_host() != (ctx.orig_host or ""):
            raise Fail("manual host was not restored")
        wait_connected(45)
        pc_clip_to_phone(ctx, new_clip(ctx, "restore"))
    except Exception as e:  # noqa: BLE001
        raise Recovery(scrub(f"{type(e).__name__}: {e}")) from None


def with_restore(ctx: Ctx, body: Callable[[], Result]) -> Result:
    """Run a pairing-altering body, always restore afterwards; if both fail, report both."""
    try:
        result = body()
    except Exception as e:  # noqa: BLE001
        try:
            restore_pairing(ctx)
        except Recovery as r:
            raise Recovery(f"{r}; original failure: {type(e).__name__}: {scrub(str(e))}") from None
        raise
    restore_pairing(ctx)
    return result


def devices_listing() -> str:
    env = dict(os.environ, PYTHONPATH=str(WINDOWS_DIR / "src"))
    return run([sys.executable, "-m", "clipbridge", "devices"], cwd=WINDOWS_DIR, env=env).stdout


# ---------------------------------------------------------------- scenarios
# Each returns (bytes moved, note) or raises Skip / Fail.

Result = tuple[int, str]


def s1(ctx: Ctx) -> Result:
    n = ctx.gen("small", 10 * 1024)
    pc_send(ctx, [n])
    wait_phone(ctx, {n: 10 * 1024}, 60)
    verify_phone(ctx, {n: n})
    return 10 * 1024, ""


def s2(ctx: Ctx) -> Result:
    n = ctx.gen("big", ctx.big)
    pc_send(ctx, [n])
    wait_phone(ctx, {n: ctx.big}, timeout_for(ctx.big))
    verify_phone(ctx, {n: n})
    return ctx.big, ""


def s3(ctx: Ctx) -> Result:
    n = ctx.gen("photo", ctx.photo, "jpg")
    hook_share(ctx, [n])
    wait_pc(ctx, {n: ctx.size(n)}, 90)
    verify_pc(ctx, {n: n})
    return ctx.size(n), ""


def s4(ctx: Ctx) -> Result:
    names = [ctx.gen(f"multi{i}", ctx.photo, "jpg") for i in range(5)] + [ctx.gen("doc", 300 * 1024, "pdf")]
    hook_share(ctx, names)
    wait_pc(ctx, {n: ctx.size(n) for n in names}, 120)
    verify_pc(ctx, {n: n for n in names})
    return sum(ctx.size(n) for n in names), ""


def s5(ctx: Ctx) -> Result:
    names = [ctx.gen("m1", 1024), ctx.gen("m2", 100 * 1024), ctx.gen("m3", 2 * MB), ctx.gen("mbig", ctx.mix_big)]
    pc_send(ctx, names)
    wait_phone(ctx, {n: ctx.size(n) for n in names}, timeout_for(ctx.mix_big))
    verify_phone(ctx, {n: n for n in names})
    return sum(ctx.size(n) for n in names), ""


def s6(ctx: Ctx) -> Result:
    n = ctx.gen("empty", 0)
    pc_send(ctx, [n])
    wait_phone(ctx, {n: 0}, 60)
    verify_phone(ctx, {n: n})
    e = ctx.gen("emptyup", 0)
    hook_share(ctx, [e])
    wait_pc(ctx, {e: 0}, 60)
    verify_pc(ctx, {e: e})
    return 0, ""


def s7(ctx: Ctx) -> Result:
    """Same name sent twice with DIFFERENT content: the second must get ' (1)' and the first must stay intact."""
    n = ctx.gen("clash", 50 * 1024)
    stem, ext = stem_ext(n)
    n2 = f"{stem} (1){ext}"
    n_b = ctx.variant(n, 50 * 1024)
    pc_send(ctx, [n])
    wait_phone(ctx, {n: 50 * 1024}, 60)
    pc_send(ctx, [n_b])
    wait_phone(ctx, {n2: 50 * 1024}, 60)
    verify_phone(ctx, {n: n, n2: n_b})
    if not ctx.hook:
        raise Skip("phone->PC half needs the hook (PC->phone half passed)")
    u = ctx.gen("clashup", 50 * 1024)
    us, ue = stem_ext(u)
    u2 = f"{us} (1){ue}"
    hook_share(ctx, [u])
    wait_pc(ctx, {u: 50 * 1024}, 60)
    u_b = ctx.variant(u, 50 * 1024)
    r = adb("push", str(ctx.path(u_b)), f"{PHONE_E2E}/{u}", timeout=60)
    if "pushed" not in r:
        raise Fail(f"adb push failed: {r.strip()[:120]}")
    hook_share(ctx, [u])
    wait_pc(ctx, {u2: 50 * 1024}, 60)
    verify_pc(ctx, {u: u, u2: u_b})
    return 200 * 1024, "PC->phone+phone->PC, second send had different content"


def s8(ctx: Ctx) -> Result:
    n = ctx.gen("big", ctx.big)
    ctx.clip_mark = ctx.log.mark()
    ctx.win_mark = winlog_pos()
    pc_send(ctx, [n])
    if not ctx.log.wait(rf"recv \w+ {ctx.big} B", ctx.clip_mark, 60):
        raise Fail("transfer never started on phone")
    t1, t2 = new_clip(ctx, "pc"), new_clip(ctx, "ph")
    ensure_moving_phone(ctx, n, ctx.big)
    d1 = pc_clip_to_phone(ctx, t1)
    ensure_moving_phone(ctx, n, ctx.big, "transfer after PC clip")
    d2 = phone_clip_to_pc(ctx, t2)
    left = ensure_moving_phone(ctx, n, ctx.big, "transfer after phone clip")
    ctx.clips = (java_hash(t1), java_hash(t2), len(t2))
    if d1 > CLIP_LIMIT_S or d2 > CLIP_LIMIT_S:
        raise Fail(f"clip too slow during transfer: pc->phone {d1:.2f}s, phone->pc {d2:.2f}s (limit {CLIP_LIMIT_S}s)")
    wait_phone(ctx, {n: ctx.big}, timeout_for(ctx.big))
    verify_phone(ctx, {n: n})
    return ctx.big, (f"clip pc->phone {d1 * 1000:.0f}ms, phone->pc {d2 * 1000:.0f}ms, "
                     f"transfer at {left * 100 // ctx.big}% after both")


def s9(ctx: Ctx) -> Result:
    if not ctx.hook:
        raise Skip("hook missing")
    # a) big PC->phone running, small phone->PC must not wait for it
    big = ctx.gen("big", ctx.big)
    small_up = ctx.gen("smallup", 20 * 1024)
    mark = ctx.log.mark()
    pc_send(ctx, [big])
    if not ctx.log.wait(rf"recv \w+ {ctx.big} B", mark, 60):
        raise Fail("big transfer never started")
    ensure_moving_phone(ctx, big, ctx.big, "big PC->phone")
    hook_share(ctx, [small_up])
    wait_pc(ctx, {small_up: 20 * 1024}, 60)
    ensure_moving_phone(ctx, big, ctx.big, "big PC->phone after the small file arrived")
    wait_phone(ctx, {big: ctx.big}, timeout_for(ctx.big))
    verify_phone(ctx, {big: big})
    verify_pc(ctx, {small_up: small_up})
    clean_dests(ctx)
    # b) big phone->PC running, small PC->phone must not wait for it
    bigup = ctx.gen("bigup", ctx.big)
    small_dn = ctx.gen("smalldn", 20 * 1024)
    hook_share(ctx, [bigup])
    ensure_moving_pc(ctx, bigup, ctx.big, "big phone->PC")
    pc_send(ctx, [small_dn])
    wait_phone(ctx, {small_dn: 20 * 1024}, 60)
    ensure_moving_pc(ctx, bigup, ctx.big, "big phone->PC after the small file arrived")
    wait_pc(ctx, {bigup: ctx.big}, timeout_for(ctx.big))
    verify_pc(ctx, {bigup: bigup})
    verify_phone(ctx, {small_dn: small_dn})
    return 2 * ctx.big, "both directions, big proven in flight before and after the small file"


def s12(ctx: Ctx) -> Result:
    """Same direction: PC->phone big + PC->phone small."""
    big = ctx.gen("big", ctx.big)
    small = ctx.gen("smallsame", 20 * 1024)
    mark = ctx.log.mark()
    pc_send(ctx, [big])
    if not ctx.log.wait(rf"recv \w+ {ctx.big} B", mark, 60):
        raise Fail("big transfer never started")
    ensure_moving_phone(ctx, big, ctx.big, "big PC->phone")
    pc_send(ctx, [small])
    t_sent = time.monotonic()
    wait_phone(ctx, {small: 20 * 1024}, timeout_for(ctx.big))
    wait_phone(ctx, {big: ctx.big}, timeout_for(ctx.big))
    verify_phone(ctx, {big: big, small: small})
    # Proof from the phone log timestamps: when did the small transfer start vs when did the big one complete?
    t_big_done = t_small_start = None
    for t, line in ctx.log.lines[mark:]:
        m = re.search(r"received (\d+) B in", line)
        if t_big_done is None and m and int(m.group(1)) >= ctx.big * 0.9:
            t_big_done = t
        if t_small_start is None and re.search(r"recv \w+ 20480 B, resuming at", line):
            t_small_start = t
    if t_big_done is None or t_small_start is None:
        raise Fail("phone log lacks the big completion line or the small transfer's start line")
    if t_sent >= t_big_done:
        raise Fail("the small file was sent after the big one had completed; not a concurrent test")
    if t_small_start > t_big_done:
        raise Xfail(f"small transfer started {t_small_start - t_big_done:.1f}s after the big one completed "
                    f"although it was sent {t_big_done - t_sent:.1f}s before that (FIFO)")
    return ctx.big, "small transfer started while the big one was still running"


def s10(ctx: Ctx) -> Result:
    n = ctx.gen("big", ctx.big)
    mark = ctx.log.mark()
    pc_send(ctx, [n])
    if not ctx.log.wait(rf"recv \w+ {ctx.big} B", mark, 60):
        raise Fail("transfer never started on phone")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 30:
        pend = phone_progress(ctx, n)
        if pend and pend >= ctx.big * 0.2:
            break
        time.sleep(0.2)
    else:
        raise Fail("transfer never reached 20%")
    sh("svc wifi disable")
    try:
        wait_not_connected(20, "link did not drop after Wi-Fi off")
        time.sleep(2)
        first = phone_progress(ctx, n)
        time.sleep(1)
        have_off = phone_progress(ctx, n)  # bytes on the phone while the link is down
    finally:
        sh("svc wifi enable")
    if not have_off or not first:
        raise Fail("no partial file on the phone while the link was down (cannot measure the resume)")
    if have_off != first:
        raise Fail("transfer kept moving while the link was supposedly down")
    if have_off >= ctx.big:
        raise Fail("transfer had already finished when the link dropped")
    wait_phone(ctx, {n: ctx.big}, timeout_for(ctx.big) + 60)
    verify_phone(ctx, {n: n})
    # PC->phone: the PC is the sender, and the PC log's `resume` line exists only for PC receives, so the phone's
    # `resuming at` line is the resume evidence here.
    resumes = [int(m.group(1)) for line in ctx.log.since(mark)
               if (m := re.search(r"recv \w+ \d+ B, resuming at (\d+) B", line))]
    if len(resumes) < 2:
        raise Fail(f"no reconnect/resume seen in phone log (recv lines: {len(resumes)})")
    resumed_at = resumes[-1]
    if resumed_at <= 0:
        raise Fail("transfer restarted from 0 after reconnect")
    if resumed_at < have_off - MB:
        raise Fail(f"resumed at {resumed_at // MB} MB but phone already had {have_off // MB} MB: bytes were re-sent")
    return ctx.big, (f"had {have_off * 100 // ctx.big}% at drop, resumed at {resumed_at * 100 // ctx.big}%, "
                     f"re-sent {(ctx.big - resumed_at) // MB} of {ctx.big // MB} MB (phone resume line)")


def s11(ctx: Ctx) -> Result:
    if not ctx.clips:
        raise Skip("needs scenario 8")
    time.sleep(3)
    h1, h2, l2 = ctx.clips
    lines = ctx.log.since(ctx.clip_mark)
    remote_h1 = sum(1 for x in lines if re.search(rf"remote clip len=\d+ hash={h1} ", x))
    sent_h1 = sum(1 for x in lines if re.search(rf"send clip len=\d+ hash={h1}\b", x))
    remote_h2 = sum(1 for x in lines if re.search(rf"remote clip len=\d+ hash={h2} ", x))
    sent_h2 = sum(1 for x in lines if re.search(rf"send clip len=\d+ hash={h2}\b", x))
    if remote_h1 != 1 or sent_h1:
        raise Fail(f"PC clip bounced: phone remote-applied {remote_h1}x, re-sent {sent_h1}x")
    if remote_h2 or sent_h2 != 1:
        raise Fail(f"phone clip bounced: echoed back {remote_h2}x, sent {sent_h2}x")
    note = "phone log clean"
    if ctx.winlog_ok:
        got = [x for x in winlog_since(ctx.win_mark) if "clip <-" in x]
        if len(got) != 1:
            raise Fail(f"Windows log shows {len(got)} 'clip <-' lines for the run, expected 1")
        note += ", Windows log clean"
    else:
        note += " (Windows log not readable here)"
    return 0, note


def s13(ctx: Ctx) -> Result:
    try:
        ps("Stop-ScheduledTask -TaskName ClipBridge")
        wait_not_connected(20, "phone did not notice the PC server stopping")
        t0 = time.monotonic()
        ps("Start-ScheduledTask -TaskName ClipBridge")
        dt = wait_connected(60, t0)
    finally:
        ensure_task_running()
    d = pc_clip_to_phone(ctx, new_clip(ctx, "restart"))
    if dt > RECONNECT_LIMIT_S:
        raise Fail(f"link back after {dt:.1f}s (limit {RECONNECT_LIMIT_S:.0f}s)")
    return 0, f"link back {dt:.1f}s after task start (limit {RECONNECT_LIMIT_S:.0f}s), clip {d * 1000:.0f}ms"


def s14(ctx: Ctx) -> Result:
    sh(f"am force-stop {PKG}")
    wait_not_connected(10, "phone still reports Connected after force-stop", allow_dead=True)
    t0 = time.monotonic()
    sh(f"am start -n {PKG}/.MainActivity")
    dt = wait_connected(45, t0)
    d = pc_clip_to_phone(ctx, new_clip(ctx, "killed"))
    sh("input keyevent KEYCODE_HOME")
    pid1 = sh(f"pidof {PKG}").strip()
    sh(f"am kill {PKG}")
    time.sleep(3)
    pid2 = sh(f"pidof {PKG}").strip()
    if pid1 and pid1 == pid2:
        if phone_state()[0] != "connected":
            raise Fail(f"service survived am kill but link dropped (status: {phone_status()})")
        kill = "survived am kill, still connected"
    else:
        back = wait_connected(RECONNECT_LIMIT_S + 15)
        kill = f"process died on am kill, link back {back:.1f}s"
    if dt > RECONNECT_LIMIT_S:
        raise Fail(f"link back {dt:.1f}s after relaunch (limit {RECONNECT_LIMIT_S:.0f}s)")
    return 0, f"relaunch -> link {dt:.1f}s, clip {d * 1000:.0f}ms; {kill}"


def s15(ctx: Ctx) -> Result:
    try:
        wifi_off()
        time.sleep(3)
        t0 = wifi_on()
        dt = wait_connected(60, t0)
    finally:
        sh("svc wifi enable")
    d = pc_clip_to_phone(ctx, new_clip(ctx, "idle"))
    if dt > RECONNECT_LIMIT_S:
        raise Fail(f"reconnect took {dt:.1f}s (limit {RECONNECT_LIMIT_S:.0f}s)")
    return 0, f"reconnect {dt:.1f}s (limit {RECONNECT_LIMIT_S:.0f}s), clip {d * 1000:.0f}ms"


def phone_ip() -> str:
    out = sh("ip -f inet addr show wlan0")
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    if not m:
        raise Fail("cannot read the phone's Wi-Fi address")
    return m.group(1)


def pc_peer_connected(ip: str) -> bool:
    """Does the PC server still hold an established connection from THIS phone (netstat, local port 8765)?"""
    out = run(["netstat", "-ano", "-p", "TCP"], timeout=30).stdout
    rx = re.compile(rf"\s*TCP\s+\S+:8765\s+{re.escape(ip)}:\d+\s+ESTABLISHED")
    return any(rx.match(line) for line in out.splitlines())


def wait_pc_dropped(ip: str, timeout: float) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if not pc_peer_connected(ip):
            return time.monotonic() - t0
        time.sleep(0.5)
    raise Fail(f"PC still holds the phone connection {timeout:.0f}s after Wi-Fi off")


def s16(ctx: Ctx) -> Result:
    """Known bug: a PC clip set right after the phone vanished goes into the dead socket and is lost."""
    a = new_clip(ctx, "gap")
    ip = phone_ip()
    pos = winlog_pos()
    try:
        wifi_off()
        peer_held = pc_peer_connected(ip)
        mark = ctx.log.mark()
        clip_set(a)
        t0 = wifi_on()
        wait_connected(60, t0)
    finally:
        sh("svc wifi enable")
    if not peer_held:
        raise Fail("PC had already dropped this phone's connection before the clip was set; cannot exercise the bug")
    if ctx.log.wait(rf"remote clip len={len(a)} hash={java_hash(a)} applied=true", mark, 10):
        return 0, "clip set while PC still held the dead peer was delivered after reconnect"
    if not ctx.winlog_ok:
        raise Skip("PC log unreadable: cannot prove the clip was captured and sent to the dead peer")
    if not any(re.search(rf"clip -> peers \(len={len(a)},", x) for x in winlog_since(pos)):
        raise Fail("PC log shows no capture of the clip, so the loss is not the known bug")
    raise Xfail("PC log shows the clip was sent to the still-held peer; it never arrived after reconnect")


def s16b(ctx: Ctx) -> Result:
    a, b = new_clip(ctx, "off1"), new_clip(ctx, "off2")
    ip = phone_ip()
    try:
        pos = winlog_pos()
        wifi_off()
        gone = wait_pc_dropped(ip, 90)
        if ctx.winlog_ok:  # authoritative: the server's own "peer disconnected" line
            end = time.monotonic() + 60
            while not any("peer disconnected" in x for x in winlog_since(pos)):
                if time.monotonic() > end:
                    raise Fail("PC log never showed 'peer disconnected'")
                time.sleep(0.5)
            how = "PC log"
        else:  # the socket vanishes from netstat before the server removes the peer (3 s was not enough, 15 s is)
            time.sleep(15)
            how = "netstat + 15 s margin; PC log unreadable here"
        mark = ctx.log.mark()
        clip_set(a)
        time.sleep(1.5)
        clip_set(b)
        time.sleep(1.5)
        t0 = wifi_on()
        dt = wait_connected(60, t0)
    finally:
        sh("svc wifi enable")
    hb = java_hash(b)
    if not ctx.log.wait(rf"remote clip len={len(b)} hash={hb} applied=true", mark, 10):
        raise Fail("latest offline clip did not arrive after reconnect")
    time.sleep(2)
    if any(f"hash={java_hash(a)} " in x and "remote clip" in x for x in ctx.log.since(mark)):
        raise Fail("the older offline clip was also delivered (single-slot should keep only the latest)")
    return 0, f"PC dropped peer {gone:.0f}s after Wi-Fi off ({how}); only the latest offline clip arrived; reconnect {dt:.1f}s"


def pick_device_row(listing: str, model: str) -> str | None:
    """The `clipbridge devices` row (id + hello name) for this phone, by its model name."""
    for line in listing.splitlines():
        if model and model.lower() in line.lower() and "(not connected yet)" not in line:
            return line
    return None


def s17(ctx: Ctx) -> Result:
    try:
        wifi_off()
        t0 = wifi_on()
        dt = wait_connected(60, t0)
    finally:
        sh("svc wifi enable")
    st = phone_status()
    if not re.match(r"Connected to \S+:\d+$", st):
        raise Fail(f"unexpected connected status format: {st!r}")
    model = sh("getprop ro.product.model").strip()
    if pick_device_row(devices_listing(), model) is None:
        raise Fail(f"clipbridge devices has no row with this phone's hello name ({model!r}); PC never completed hello")
    return 0, f"phone 'Connected to <endpoint>' after {dt:.1f}s; PC lists this phone ({model}) with its hello name"


def _rejected(ctx: Ctx, tag: str, pc_line: str, pos: int) -> str:
    """Link must go down and stay down; a PC clip must not pass; the PC log shows the rejection when readable."""
    wait_not_connected(20, "phone still Connected after bad credentials were set")
    time.sleep(6)
    kind, st = phone_state()
    if kind == "connected":
        raise Fail("phone connected with bad credentials")
    if kind != "disconnected":
        raise Fail(f"cannot confirm the disconnect: phone state {kind} ({st})")
    mark = ctx.log.mark()
    t = new_clip(ctx, tag)
    clip_set(t)
    time.sleep(4)
    if any("remote clip" in x and f"hash={java_hash(t)} " in x for x in ctx.log.since(mark)):
        raise Fail("a clip passed on a rejected link")
    if ctx.winlog_ok and not any(pc_line in x for x in winlog_since(pos)):
        raise Fail(f"PC log has no '{pc_line}' line")
    return st


def s18(ctx: Ctx) -> Result:
    def body() -> Result:
        try:
            pair_phone(host=DEAD_HOST)
            time.sleep(2)
            saved = phone_manual_host()
            if saved != DEAD_HOST:
                raise Fail(f"manual host was not saved on the phone (prefs say {saved!r})")
            wifi_off()
            time.sleep(2)
            t0 = wifi_on()
            seen: list[str] = []
            dt = wait_connected(60, t0, seen)
        finally:
            sh("svc wifi enable")
        if not any(DEAD_HOST in x for x in seen):
            raise Fail(f"the dead manual host was never dialed (no status naming {DEAD_HOST}); link up in {dt:.1f}s")
        if dt > MANUAL_DEAD_LIMIT_S:
            raise Fail(f"link up only after {dt:.1f}s with a dead manual host (limit {MANUAL_DEAD_LIMIT_S:.0f}s)")
        return 0, (f"manual host saved (prefs verified), dead dial seen in status; link up {dt:.1f}s after Wi-Fi on "
                   f"(limit {MANUAL_DEAD_LIMIT_S:.0f}s); restored")

    return with_restore(ctx, body)


def _bad_creds(ctx: Ctx, label: str, tag: str, pc_line: str, device: str) -> Result:
    def body() -> Result:
        pos = winlog_pos()  # before the pairing change, so the rejection line cannot be missed
        wrong = base64.b64encode(os.urandom(32)).decode("ascii")
        SECRETS.append(wrong)
        pair_phone(device, wrong)
        st = _rejected(ctx, tag, pc_line, pos)
        return 0, f"{label}: no link, no clip (phone status '{st}'); PC log shows the rejection; restored"

    result = with_restore(ctx, body)
    if not ctx.winlog_ok:
        raise Skip(f"{label}: phone side verified and restored, but the PC log is unreadable here so the rejection is not asserted")
    return result


def s19(ctx: Ctx) -> Result:
    assert ctx.real
    return _bad_creds(ctx, "wrong PSK", "badpsk", "auth failed (bad MAC", ctx.real[0])


def s20(ctx: Ctx) -> Result:
    assert ctx.real
    sys.path.insert(0, str(WINDOWS_DIR / "src"))
    from clipbridge import config  # noqa: PLC0415

    return _bad_creds(ctx, "unknown device", "unknown", "auth failed (unknown device", config.new_device_id())


SCENARIOS: list[tuple[str, str, Callable[[Ctx], Result]]] = [
    ("1", "PC->phone small 10 KB", s1),
    ("2", "PC->phone big", s2),
    ("3", "phone->PC photo", s3),
    ("4", "phone->PC 5 photos + PDF", s4),
    ("5", "PC->phone mixed small + big", s5),
    ("6", "empty file both ways", s6),
    ("7", "name clash (1) suffix", s7),
    ("8", "clips during big transfer", s8),
    ("9", "small file during big, both ways", s9),
    ("10", "Wi-Fi off/on mid-transfer resume", s10),
    ("11", "clip ping-pong", s11),
    ("12", "small during big, same direction", s12),
    ("13", "PC server restart", s13),
    ("14", "app force-stop / am kill", s14),
    ("15", "idle Wi-Fi off/on reconnect", s15),
    ("16", "clip right after phone vanished", s16),
    ("16b", "offline clip flush (latest only)", s16b),
    ("17", "connected state + hello visible", s17),
    ("18", "dead manual host falls through", s18),
    ("19", "wrong PSK rejected", s19),
    ("20", "unknown device rejected", s20),
]
PAIRING = {"18", "19", "20"}  # change the phone's pairing; only run with --pairing, always restored
XFAIL: dict[str, str] = {}  # known product bugs: must fail with the symptom; passing unexpectedly is XPASS
DEPS = {"11": ["8"]}  # scenarios that need another one's data


# ---------------------------------------------------------------- preconditions

def preconditions(ctx: Ctx) -> None:
    def bad(msg: str) -> None:
        raise SystemExit(f"PRECONDITION FAILED: {msg}")

    devs = [l.split()[0] for l in adb("devices").splitlines()[1:] if l.strip().endswith("device")]
    if len(devs) != 1:
        bad(f"expected exactly 1 adb device in 'device' state, found {len(devs)}")
    if "isKeyguardShowing=true" in sh("dumpsys window | grep -i isKeyguardShowing"):
        bad("phone is locked; unlock the screen")
    if "mWakefulness=Awake" not in sh("dumpsys power | grep mWakefulness="):
        bad("phone screen is off; wake and unlock it")
    if "BridgeService" not in sh(f"dumpsys activity services {PKG}") or "isForeground=true" not in sh(
            f"dumpsys activity services {PKG}"):
        bad("ClipBridge service is not running on the phone; open the app")
    task = run(["powershell", "-NoProfile", "-Command", "(Get-ScheduledTask ClipBridge).State"]).stdout.strip()
    if task != "Running":
        bad(f"PC task 'ClipBridge' is {task or 'missing'}; run Start-ScheduledTask ClipBridge")
    ctx.hook = "E2eShareActivity" in sh(f"cmd package resolve-activity --brief -n {PKG}/.debug.E2eShareActivity")
    if not ctx.hook and "DEBUGGABLE" in sh(f"dumpsys package {PKG} | grep pkgFlags"):
        bad("installed app is a debug build WITHOUT the E2eShareActivity hook (stale APK); "
            "run :app:installDebug from the current tree")
    # link probe: clip round trip both ways (this is what 'link up' means)
    pos = winlog_pos()
    try:
        pc_clip_to_phone(ctx, new_clip(ctx, "probe1"))
        phone_clip_to_pc(ctx, new_clip(ctx, "probe2"))
    except Fail as e:
        bad(f"link is not up ({e}); on the phone open the app, check Wi-Fi; restart PC via Start-ScheduledTask ClipBridge")
    time.sleep(0.5)
    ctx.winlog_ok = winlog_pos() > pos
    sh(f"mkdir -p {PHONE_DIR} {PHONE_E2E} && chmod 777 {PHONE_E2E}")  # the app must be able to list it
    PC_DIR.mkdir(parents=True, exist_ok=True)
    ctx.partial_names = {p.name for p in PARTIAL_DIR.glob("*")} if PARTIAL_DIR.exists() else set()
    ctx.ready = True
    print(f"preconditions ok: device {devs[0]}, hook {'present' if ctx.hook else 'MISSING (release build: phone->PC file scenarios skip)'}, "
          f"Windows log {'readable' if ctx.winlog_ok else 'not readable'}")


def fmt_row(num: str, title: str, status: str, size: int, secs: float, note: str) -> str:
    sz = f"{size / MB:.1f} MB" if size >= MB else f"{size // 1024} KB" if size else "0 B"
    rate = f"{size / MB / secs:.1f} MB/s" if size >= MB and secs > 0 else "-"
    return f"{num:>3}  {status:<13}  {title:<36} {sz:>9} {secs:7.1f}s {rate:>10}  {note}"


def ensure_wifi_on() -> None:
    sh("svc wifi enable")
    for _ in range(20):
        if sh("settings get global wifi_on").strip() == "1":
            return
        time.sleep(0.5)
    raise Fail("Wi-Fi is not on")


def ensure_app_running() -> None:
    if sh(f"pidof {PKG}").strip():
        return
    sh(f"am start -n {PKG}/.MainActivity")
    for _ in range(20):
        if sh(f"pidof {PKG}").strip():
            return
        time.sleep(0.5)
    raise Fail("app could not be relaunched")


def final_cleanup(ctx: Ctx, saved_clip: str | None) -> list[str]:
    """Every step independent; returns the list of problems (never raises)."""
    problems: list[str] = []

    def step(name: str, fn: Callable[[], object]) -> None:
        try:
            fn()
        except BaseException as e:  # noqa: BLE001
            problems.append(f"{name}: {type(e).__name__}: {scrub(str(e))}")

    step("Wi-Fi on", ensure_wifi_on)
    step("PC task running", lambda: ensure_task_running(wait=30))
    step("app running", ensure_app_running)
    if ctx.ready:
        step("link up", lambda: wait_connected(45))
    step("test files on PC/phone", lambda: clean_dests(ctx))
    step("phone e2e dir", lambda: sh(f"rm -f {PHONE_E2E}/e2e{ctx.rid}_* 2>/dev/null"))
    step("logcat reader", ctx.log.close)
    step("temp dir", lambda: shutil.rmtree(ctx.tmp, ignore_errors=True))
    if saved_clip is not None:
        step("clipboard restore", lambda: clip_set(saved_clip))
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="shrink big files (128 MB instead of 1 GB)")
    ap.add_argument("--only", help="comma-separated scenario ids (prerequisites run too)")
    ap.add_argument("--pairing", action="store_true", help="run only the pairing scenarios 18-20 (they alter and restore the phone's pairing)")
    ap.add_argument("--allow-skip", action="store_true", help="skipped scenarios do not fail the run")
    args = ap.parse_args()
    only = {x.strip() for x in args.only.split(",")} if args.only else None
    known = {n for n, _, _ in SCENARIOS}
    if only:
        unknown = only - known
        if unknown:
            raise SystemExit(f"unknown scenario id(s): {', '.join(sorted(unknown))}; known: {', '.join(n for n, _, _ in SCENARIOS)}")
        if (only & PAIRING) and not args.pairing:
            raise SystemExit("scenarios 18-20 alter the phone's pairing: pass --pairing")
        for n in list(only):
            only.update(DEPS.get(n, []))
    selected = [(n, t, f) for n, t, f in SCENARIOS if (n in PAIRING) == args.pairing and (not only or n in only)]
    if not selected:
        raise SystemExit("no scenarios selected (--pairing runs only 18-20; without it 18-20 never run)")

    rid = f"{random.randrange(16 ** 4):04x}"
    tmp = Path(tempfile.mkdtemp(prefix="clipbridge_e2e_"))
    log = LogTail()
    ctx = Ctx(args.quick, rid, tmp, log)
    saved_clip = clip_get()
    bad_rows = 0
    count = 0
    recovery_failed = False
    try:
        preconditions(ctx)
        if args.pairing:
            ctx.real = real_pairing()
            ctx.orig_host = phone_manual_host()
            if ctx.orig_host is None:
                raise SystemExit("PRECONDITION FAILED: cannot read the phone's saved manual host (run-as needs a debug build)")
        for num, title, fn in selected:
            t0 = time.monotonic()
            size, note, status = 0, "", "PASS"
            try:
                size, note = fn(ctx)
                if num in XFAIL:
                    status = "XPASS"
                    note += " (expected failure did not occur: remove from XFAIL)"
            except Recovery as e:
                print(scrub(f"RECOVERY: the phone may be left with wrong pairing ({e}). In your own PowerShell run: "
                            "python -m clipbridge pair"), flush=True)
                status, note = "FAIL", "restore failed"
                recovery_failed = True
            except Skip as e:
                status, note = "SKIP", str(e)
            except Xfail as e:
                status, note = (f"XFAIL ({XFAIL[num]})" if num in XFAIL else "FAIL"), str(e)
            except Fail as e:
                status, note = "FAIL", scrub(str(e))
            except Exception as e:  # noqa: BLE001 - report any crash as a failure and keep going
                status, note = "FAIL", scrub(f"{type(e).__name__}: {e}")
            secs = time.monotonic() - t0
            count += 1
            if status in ("FAIL", "XPASS") or (status == "SKIP" and not args.allow_skip):
                bad_rows += 1
            print(fmt_row(num, title, status, size, secs, note), flush=True)
            try:
                clean_dests(ctx)
            except Exception:  # noqa: BLE001 - final cleanup retries and reports
                pass
            if recovery_failed:
                break
    finally:
        problems = final_cleanup(ctx, saved_clip)
        if problems:
            print("CLEANUP PROBLEMS: " + "; ".join(problems), flush=True)
    print(f"\n{count - bad_rows} of {count} as expected, {bad_rows} failed/skipped/unexpected")
    return 1 if (bad_rows or problems) else 0


if __name__ == "__main__":
    sys.exit(main())
