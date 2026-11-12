# ClipBridge

Zero-cloud, LAN-only secure link between a Windows laptop (server) and Android devices (clients).
Clipboard sync is the first feature on that link. Full plan: `docs/PLAN.md`. Wire spec: `protocol/PROTOCOL.md`.

## Layout
- `windows/` Python >= 3.10 package `clipbridge` (`src/clipbridge/link/`, `src/clipbridge/features/`), tests in `windows/tests/`.
- `android/` Kotlin app, single `app` module, package `com.clipbridge` (`link.*`, `features.clipboard.*`).
- `protocol/` `PROTOCOL.md` (normative), `gen_vectors.py`, `test_vectors.json` (shared by both test suites).
- `docs/` plan and notes. `spike/` is a throwaway experiment: ignored, never committed.

## Layering rule (most important)
- **Link layer**: discovery, connect/reconnect, handshake, crypto, liveness, peer registry. Knows nothing about features.
- **Feature layer**: one module per feature (`name`, `on_peer_connected`, `on_peer_disconnected`, `on_message(peer, header, body)`), uses `send(peer|all, header, body)`.
- Link routes by `header.ch`. Never put feature code or feature names inside the link layer. A new feature = new module + one register line per side.

## Commands
- Python tests: `pytest windows/tests`
- Regenerate vectors: `python protocol/gen_vectors.py` (deterministic; must not change the file unless the spec changes)
- Android unit tests (PowerShell):
  `$env:JAVA_HOME="C:\Program Files\Android\Android Studio\jbr"; android/gradlew :app:testDebugUnitTest`
  (system Java is too new for AGP; always use the Android Studio JBR)

## Conventions
- Typed code (Python type hints everywhere; Kotlin explicit public types). Small, single-purpose modules.
- Pure logic (crypto, envelope, SyncEngine) has no I/O and is unit-tested; I/O stays at the edges.
- Protocol changes: edit `PROTOCOL.md` first, regenerate vectors, then both implementations.
- Never log PSKs, keys, or clipboard contents (log lengths/hashes only).
- No over-engineering: build only what the current step of `docs/PLAN.md` needs.

## Working in sessions
Each build step in `docs/PLAN.md` runs in its own fresh session to save usage.
Read only the files the step touches; this file + `PROTOCOL.md` are the shared context.
