"""Wiring: logging, single-instance mutex, link + discovery + clipboard feature on one asyncio loop."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys

import pywintypes
import win32api
import win32event
import winerror

from clipbridge import config
from clipbridge.features.clipboard.feature import ClipboardFeature
from clipbridge.features.clipboard.win32 import ClipboardThread
from clipbridge.link.discovery import Advertiser
from clipbridge.link.server import DEFAULT_PORT, Link

MUTEX_NAME = "Local\\ClipBridge"
log = logging.getLogger("clipbridge")


def setup_logging() -> None:
    directory = config.log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(
            directory / "clipbridge.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
    ]
    if sys.stderr is not None:  # pythonw has no stderr
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def acquire_mutex() -> object | None:
    """Return the mutex handle (keep it alive) or None if another instance runs."""
    handle = win32event.CreateMutex(None, False, MUTEX_NAME)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        return None
    return handle


def is_running() -> bool:
    try:
        handle = win32event.OpenMutex(win32event.SYNCHRONIZE, False, MUTEX_NAME)
    except pywintypes.error:
        return False
    win32api.CloseHandle(handle)
    return True


async def _run(port: int) -> None:
    loop = asyncio.get_running_loop()
    psk = config.get_or_create_psk()
    link = Link(psk, port=port)
    advertiser = Advertiser(port)

    clip_thread: ClipboardThread
    feature = ClipboardFeature(
        link, lambda text: clip_thread.set_text(text), lambda png: clip_thread.set_image(png)
    )

    def on_text(text: str) -> None:
        asyncio.run_coroutine_threadsafe(feature.on_local_text(text), loop)

    def on_image(png: bytes) -> None:
        asyncio.run_coroutine_threadsafe(feature.on_local_image(png), loop)

    clip_thread = ClipboardThread(
        on_text, lambda: loop.call_soon_threadsafe(advertiser.request_refresh), on_image
    )
    link.register(feature)

    await link.start()
    try:
        await advertiser.start()
    except Exception:
        log.exception("mDNS start failed; the 30 s watcher will retry")
    clip_thread.start_and_wait()
    watcher = asyncio.create_task(advertiser.watch())
    try:
        await asyncio.Event().wait()
    finally:
        watcher.cancel()
        clip_thread.stop()
        await advertiser.stop()
        await link.stop()


def run(port: int = DEFAULT_PORT) -> int:
    setup_logging()
    mutex = acquire_mutex()
    if mutex is None:
        log.info("another instance is running; exiting")
        return 0
    log.info("ClipBridge starting")
    try:
        asyncio.run(_run(port))
    except KeyboardInterrupt:
        log.info("stopped")
        return 0
    except Exception:
        log.exception("fatal error")
        return 1
    return 0
