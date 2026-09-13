#!/usr/bin/env bash
# Geraeteseite gegen den Halo-Emulator des Herstellers. Python samt lupa liegt
# nicht auf dem Host, deshalb im Container. Das Image wird beim ersten Lauf
# gebaut (danach im Cache).
set -e
cd "$(dirname "$0")/../.."
docker build -q -t katagent-halo-emu - <<'DOCKER'
FROM python:3.12-slim
RUN pip install --no-cache-dir halo-emulator
ENV SDL_VIDEODRIVER=dummy
DOCKER
docker run --rm -v "$PWD:/w" -w /w katagent-halo-emu python tools/halo/emu_test.py
