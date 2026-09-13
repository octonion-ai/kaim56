# KatAgent (Android)

Small Android app with **two modes**:

1. **Server agent** — chats with a running Firecracker agent through the manager:
   `POST {server-url}/i/{instance}/api/chat` (body `{"message": …}` → `{"reply": …}`), basic auth.
2. **On-device (Gemma)** — runs a **Gemma model locally** on the phone (MediaPipe LLM Inference, offline, works without network/VPN too, e.g. on the go).

## Building

Layout: this folder is the Gradle root project (`settings.gradle.kts`, plugins),
`app/` the module, `iroh-android/` the native iroh module (Rust) whose shared
library and UniFFI Kotlin bindings the module needs before Gradle runs.

Without a local Android SDK, via Docker:
```bash
cd app
./iroh-android/build-android.sh                               # once, and after lib.rs changes
docker build -f Dockerfile.build -t katagent-build .          # once (Android SDK + Gradle)
docker run --rm -v "$PWD":/project -v katagent-gradle:/root/.gradle katagent-build \
  gradle assembleDebug --no-daemon --console=plain
# Result: app/build/outputs/apk/debug/app-debug.apk — ship it as katagent-<versionName>.apk
```

CI: `.github/workflows/apk.yml` builds the native module and the APK on every
`v*` tag and attaches `katagent-<versionName>.apk` to the release. The APK is
signed with the project's stable key from the repository secret
`KATAGENT_KEYSTORE_B64` (base64 of `keystore/katagent.jks`, gitignored); without
the secret the workflow signs with a throwaway key and only keeps an artifact,
because such an APK could not update an installed KatAgent in place.

## Chat sync
Chats live in the manager's shared store (`/api/chats`) and are reconciled
**live**: the app hangs on a long-poll (`?since=<rev>&wait=25`), the manager
responds as soon as the app or the web UI writes. New messages from the other
side thus appear within a fraction of a second — without a restart. The manager
merges server-side per chat `id` (newer `updatedAt` wins); deletions do not
sync (there are no tombstones).

## Installing
Copy the debug APK to the phone and open it → "Allow from unknown sources" → install.
(Or via `adb install app-debug.apk`.)

## Configuration (gear icon, top right)
- **Server URL**: `https://agents.example.com` (reachable only on the home network/VPN).
- **Instance**: name of a **running** instance in the manager (e.g. an openrouter/pi web instance). Create & start it in the manager first.
- **User/password**: the manager's basic auth (`admin` / …).
- **Pick a Gemma .task model**: a `.task` file (see below). It is copied into app storage and loaded.

## On-device model (Gemma, `.task`)
MediaPipe needs a **`.task` bundle**. Suitable models (save to `Downloads` on the phone, then pick them in the settings):
- Google AI Edge / LiteRT Community (HuggingFace `litert-community`) or Kaggle "Gemma" — e.g.
  `gemma-3n-E2B-it` / `gemma-3n-E4B-it` or `gemma2-2b-it` as `.task` (CPU/GPU, int4/int8).
- Size depending on the variant ~1–4 GB. An int4 variant fits the Xiaomi 15 (arm64) well.

Note: plain `.gguf` models do **not** work directly — MediaPipe expects the `.task` format.

## Architecture
- `MainActivity.kt` — Compose UI (chat, mode switch, settings, model picker via SAF).
- `ServerAgent.kt` — HTTP client for the manager agent.
- `LocalGemma.kt` — MediaPipe `LlmInference` wrapper (load/generate).
- `Prefs.kt` — settings (SharedPreferences).
- `Dockerfile.build` — reproducible Android build environment.

## Known MVP limits (future stages)
- Replies arrive as a whole (no token streaming) — streaming could be added.
- No chat-history persistence, no model download in the app (file selection only).
- Server mode needs the home network/VPN; on-device runs everywhere, offline.
