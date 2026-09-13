#!/usr/bin/env bash
# Cross-compile the native iroh module for Android and generate the UniFFI
# Kotlin bindings, placing both into the app source tree so `gradle
# assembleDebug` packages them. Run once (and after any change to lib.rs).
#
#   ./build-android.sh              # arm64 + armv7 + x86_64
#
# Output:
#   app/app/src/main/jniLibs/<abi>/libkaim_iroh.so
#   app/app/src/main/kotlin/uniffi/kaim_iroh/kaim_iroh.kt
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$HERE/../app" && pwd)"             # the Gradle module app/app
JNILIBS="$APP/src/main/jniLibs"
KOTLIN="$APP/src/main/kotlin"

docker build -q -t kaim-iroh-android -f "$HERE/Dockerfile.android" "$HERE"

docker run --rm -v "$HERE":/work -v "$APP":/app -w /work \
  -v katfs_node_registry:/usr/local/cargo/registry \
  -v kaim_iroh_android_target:/work/target \
  kaim-iroh-android bash -eux -c '
    # 1) per-ABI shared libraries for the APK
    cargo ndk -t arm64-v8a -o /app/src/main/jniLibs build --release
    # 2) a host build of the cdylib, used only to extract UniFFI metadata
    cargo build --release
    # 3) generate the Kotlin bindings from that library
    cargo run --release --bin uniffi-bindgen -- generate \
      --library target/release/libkaim_iroh.so --language kotlin \
      --out-dir /app/src/main/kotlin
    chown -R '"$(id -u)":"$(id -g)"' /app/src/main/jniLibs /app/src/main/kotlin/uniffi 2>/dev/null || true
  '
find "$JNILIBS" -name "libiroh*.so" -delete   # keep only libkaim_iroh.so (self-contained)
echo "built jniLibs + Kotlin bindings:"
ls -R "$JNILIBS" 2>/dev/null | head
ls "$KOTLIN/uniffi/kaim_iroh/" 2>/dev/null || true
