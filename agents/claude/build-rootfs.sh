#!/usr/bin/env bash
# Baut NUR das Gast-Rootfs (rootfs.ext4) und legt es als claude-Instanz ab.
# Keine Downloads (firecracker/vmlinux liegen schon in ../../firecracker/bin).
# Bewusst OHNE 'set -e' -> zeigt jeden Schritt/Fehler statt still abzubrechen.
FC_DIR="${FC_DIR:-$(cd "$(dirname "$0")/.." && pwd)/firecracker}"   # the manager tree next to this folder
cd "$(dirname "$0")"
INST="${FC_DIR}"/instances/claude-rootfs.ext4
fail(){ echo "❌ FEHLER in: $1"; exit 1; }

echo "== [1] bridge.py + web_bridge.py bereitstellen =="
# Die Quellen lagen frueher in Nachbarordnern; heute sind die hiesigen Dateien
# der Stand. Nur kopieren, wenn es die alten Quellen noch gibt — sonst die
# lokalen nehmen (und nur meckern, wenn auch die fehlen).
[ -f ../claude-signal-bridge/bridge.py ] && cp ../claude-signal-bridge/bridge.py ./bridge.py
[ -f ../agent-web/web_bridge.py ] && cp ../agent-web/web_bridge.py ./web_bridge.py
[ -f ./bridge.py ] || fail "bridge.py fehlt"
[ -f ./web_bridge.py ] || fail "web_bridge.py fehlt"

echo "== [2] docker build (kann dauern: node + claude-code) =="
docker build -f Dockerfile.rootfs -t claude-fc-rootfs . || fail "docker build"

echo "== [3] Rootfs exportieren =="
rm -rf rootfs && mkdir -p rootfs
CID=$(docker create claude-fc-rootfs) || fail "docker create"
docker export "$CID" | tar -C rootfs -xf - || fail "docker export"
docker rm "$CID" >/dev/null

echo "== [4] Workspace einlegen =="
# Keine Anmeldung ins Image: guest-init holt das lebende Host-Credential beim
# Boot vom Manager, die Bruecke zieht es vor jedem Turn nach. Ein Token im
# Image waere nach Stunden tot und laege fuer immer in der Datei.
mkdir -p rootfs/home/node/.claude rootfs/home/node/workspace

echo "== [5] ext4-Image bauen =="
SIZE_MB=${ROOTFS_MB:-3072}
rm -f rootfs.ext4
truncate -s "${SIZE_MB}M" rootfs.ext4 || fail "truncate"
mkfs.ext4 -F -q -d rootfs rootfs.ext4 || fail "mkfs.ext4 -d (e2fsprogs >=1.43 nötig)"
rm -rf rootfs

echo "== [6] als claude-Instanz ablegen =="
cp rootfs.ext4 "$INST" || fail "kopieren nach instances/"
echo "✅ FERTIG:"; ls -lh "$INST"
