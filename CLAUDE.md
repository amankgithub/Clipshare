# ClipBridge

Zero-cloud, LAN-only secure link between a Windows laptop (server) and Android devices (clients).
Features on that link: clipboard sync and file transfer (`files`). Full plan: `docs/PLAN.md`. Wire spec: `protocol/PROTOCOL.md`.

## Layout
- `windows/` Python >= 3.10 package `clipbridge` (`src/clipbridge/link/`, `src/clipbridge/features/`), tests in `windows/tests/`,
  device runner in `windows/e2e/` (drives the real PC task and the phone over adb; not part of pytest).
- `android/` Kotlin app, single `app` module, package `com.clipbridge` (`link.*`, `features.<name>.*`).
- `protocol/` `PROTOCOL.md` (normative), `gen_vectors.py`, `test_vectors.json` (shared by both test suites).
- `docs/` plan, `FOOTPRINT.md` (everything installed on either device + how to remove it). `spike/` is a throwaway experiment: ignored, never committed.

## Layering rule (most important)
- **Link layer**: discovery, connect/reconnect, handshake, crypto, liveness, peer registry, and general
  services: device registry (per-device keys), `hello` (peer name, features, versions), request/reply
  (delivery receipts are the empty reply), file transfer (data connections). Knows nothing about features.
- **Feature layer**: one module per feature (`name`, `on_peer_connected`, `on_peer_disconnected`, `on_message(peer, header, body)`), uses `send(peer|all, header, body)`;
  features that take files add `on_file_offer` / `on_file_received` and use `send_file`.
- Link routes by `header.ch`. Never put feature code or feature names inside the link layer. A new feature = new module + one register line per side.
- Generic needs (request/reply, file transfer, ...) belong in the link layer as
  general services any feature can use, never as one feature's special case.

## Adding or changing a feature
Write it as if the project had been designed with it from day one:
- Put it where the design says it belongs. If the current structure has no clean place for it,
  restructure first (move or generalize code), then add the feature. Never bolt on a special case.
- Update `PROTOCOL.md`, `docs/PLAN.md`, and this file so they read as one design, not a changelog.
  No "added later", "v2 hack", or "new:" notes in code or docs.
- Delete code, flags, and docs the change makes obsolete. No dead paths or compatibility shims for
  builds that only ever ran on your own devices (both sides ship together).
- Leave the code the next agent would expect: same naming, same module shape as its siblings.

## Commands
- Python tests: `pytest windows/tests`
- Regenerate vectors: `python protocol/gen_vectors.py` (deterministic; must not change the file unless the spec changes)
- Android unit tests (PowerShell):
  `$env:JAVA_HOME="C:\Program Files\Android\Android Studio\jbr"; android/gradlew :app:testDebugUnitTest`
  (system Java is too new for AGP; always use the Android Studio JBR). Includes the Kotlin↔Python interop
  tests, which start `windows/tests/interop_server.py` with `windows/.venv` (skipped if the venv is missing).
- Device run (vivo on USB, unlocked, debug APK installed, `ClipBridge` task running), from a normal terminal
  (the tool shell virtualizes `%LOCALAPPDATA%` and cannot read the PC log):
  `windows/.venv/Scripts/python.exe windows/e2e/device.py [--quick] [--pairing] [--only N]`

## Conventions
- Typed code (Python type hints everywhere; Kotlin explicit public types). Small, single-purpose modules.
- Pure logic (crypto, envelope, SyncEngine) has no I/O and is unit-tested; I/O stays at the edges.
- Protocol changes: edit `PROTOCOL.md` first, regenerate vectors, then both implementations.
- Never log PSKs, keys, or clipboard contents (log lengths/hashes only).
- No over-engineering: build only what the current step of `docs/PLAN.md` needs.
- No new dependencies without listing them in `docs/FOOTPRINT.md` with their removal step. Python deps
  live only in `windows/.venv`; anything written outside the repo (registry, shortcuts, AppData) must be
  undone by `clipbridge uninstall [--purge]`.

## Working in sessions
Each build step in `docs/PLAN.md` runs in its own fresh session to save usage.
Read only the files the step touches; this file + `PROTOCOL.md` are the shared context.
