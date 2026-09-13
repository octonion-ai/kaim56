#!/usr/bin/env bash
# Testet die Geraeteseite der Brille ohne Hardware. Lua liegt nicht auf dem
# Host, deshalb im Container (alpine + lua5.4).
set -e
cd "$(dirname "$0")/../.."
docker run --rm -v "$PWD:/w" -w /w alpine:latest \
  sh -c "apk add --no-cache lua5.4 >/dev/null 2>&1 && lua5.4 tools/halo/test_frame_app.lua"
