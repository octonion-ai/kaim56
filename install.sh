#!/bin/sh
# kAIm56 — installer for a fresh machine.
#
#   curl -fsSL https://raw.githubusercontent.com/<user>/kaim56/main/install.sh | sh
#   (or from a clone:  ./install.sh)
#
# What it does: check prerequisites, fetch the repo (if needed), lay out the
# runtime tree under $KAIM56_BASE, download the Firecracker binary, obtain the
# guest kernel (vmlinux), build the rootfs/containers, install the systemd
# service, run a smoke test. Idempotent: a second run updates.
#
# Options / env:
#   --check            only check prerequisites, install nothing
#   --files-only       only layout+binaries (no build, no service) — for tests/updates
#   --no-build         skip the Docker builds (files/service only)
#   --with-voice       also install the voice service (STT/TTS, ~2 GB Docker build)
#   --with-agents      also build the pi/prime/claude rootfs (large)
#   --with-hindsight   also run Hindsight (vectorize.io) as an optional second memory
#                      (container on 127.0.0.1:8888, LLM through the manager's key proxy;
#                      switch it on with HINDSIGHT_URL=http://127.0.0.1:8888 in Settings)
#   --release          update the clone to the newest release tag first (what the
#                      Update button in the web UI runs, via kaim56-update.service)
#   KAIM56_BASE=<dir>  target directory (default: $HOME)
#   KAIM56_USER=<user> operator whose tree this is when running as root (the update unit)
#   VMLINUX_URL=<url>  guest kernel to download (default: the Firecracker CI kernel 6.1.128)
#   GUEST_DNS=<ip>     DNS for the microVMs (default: 1.1.1.1)
#   REPO_URL=<url>     git source (default: github kaim56)
set -eu

FC_VERSION="v1.16.1"                       # same version as the reference installation
# Guest kernel: the kernel Firecracker's own CI boots (public S3 bucket of the
# project), so nothing has to be built or redistributed here. Override with
# VMLINUX_URL=… or drop a vmlinux into $FC_DIR/bin/ beforehand.
VMLINUX_URL="${VMLINUX_URL:-https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.12/x86_64/vmlinux-6.1.128}"
REPO_URL="${REPO_URL:-https://github.com/octonion-ai/kaim56.git}"
BASE="${KAIM56_BASE:-$HOME}"
FC_DIR="$BASE/firecracker"
GUEST_DNS="${GUEST_DNS:-1.1.1.1}"
CHECK_ONLY=0; NO_BUILD=0; WITH_VOICE=0; WITH_AGENTS=0; FILES_ONLY=0; RELEASE=0; WITH_HINDSIGHT=0
for a in "$@"; do case "$a" in
  --check) CHECK_ONLY=1;;
  --release) RELEASE=1;;
  --files-only) FILES_ONLY=1; NO_BUILD=1;;
  --no-build) NO_BUILD=1;;
  --with-voice) WITH_VOICE=1;;
  --with-agents) WITH_AGENTS=1;;
  --with-hindsight) WITH_HINDSIGHT=1;;
  *) echo "unknown option: $a"; exit 2;;
esac; done

say()  { printf '\033[1m== %s\033[0m\n' "$*"; }
fail() { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
# Run as the operator (sudo for the root steps) or as root from the update
# unit: then sudo is a no-op and git runs as the operator, so the clone stays
# theirs.
OP_USER="${KAIM56_USER:-$(id -un)}"
if [ "$(id -u)" = 0 ]; then SUDO=""; else SUDO="sudo"; fi
as_op() { if [ "$(id -u)" = 0 ] && [ "$OP_USER" != root ]; then runuser -u "$OP_USER" -- "$@"; else "$@"; fi; }

# ── [1] Prerequisites ────────────────────────────────────────────────────────
say "[1/7] Check prerequisites"
[ "$(uname -m)" = "x86_64" ] || fail "x86_64 required (is: $(uname -m))"
[ -e /dev/kvm ] || fail "/dev/kvm missing — enable KVM (BIOS/nested virt); no microVMs without KVM"
[ -w /dev/kvm ] || echo "  note: /dev/kvm not writable for $(id -un) — manager runs as root, ok"
command -v python3 >/dev/null || fail "python3 missing"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' || fail "python3 >= 3.9 required"
command -v docker >/dev/null || fail "docker missing (needed for rootfs/service builds)"
docker info >/dev/null 2>&1 || fail "docker daemon not reachable (group 'docker'? sudo?)"
command -v git >/dev/null || fail "git missing"
command -v curl >/dev/null || fail "curl missing"
command -v iptables >/dev/null 2>&1 || [ -x /sbin/iptables ] || [ -x /usr/sbin/iptables ] || fail "iptables missing (NAT for the guests)"
MKFS="$(command -v mkfs.ext4 || echo /sbin/mkfs.ext4)"; [ -x "$MKFS" ] || fail "mkfs.ext4 missing (e2fsprogs)"
command -v systemctl >/dev/null || fail "systemd required (service installation)"
command -v rsync >/dev/null || fail "rsync missing"
echo "  all present."
[ "$CHECK_ONLY" = "1" ] && { say "Check ok — installation would go to $BASE."; exit 0; }

# ── [2] Source code ──────────────────────────────────────────────────────────
say "[2/7] Source code"
SELF_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || true)"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/manager/manager.py" ]; then
  SRC="$SELF_DIR"; echo "  using local clone: $SRC"
else
  SRC="$BASE/kaim56"
  if [ -d "$SRC/.git" ]; then [ "$RELEASE" = 1 ] || as_op git -C "$SRC" pull --ff-only
  else as_op git clone "$REPO_URL" "$SRC"; fi
fi
if [ "$RELEASE" = 1 ]; then
  # Newest release tag, never a moving branch; a clone with local changes is
  # left alone rather than silently switched.
  [ -d "$SRC/.git" ] || fail "--release needs a git clone at $SRC"
  [ -z "$(as_op git -C "$SRC" status --porcelain)" ] || fail "$SRC has local changes — update it by hand"
  as_op git -C "$SRC" fetch -q --tags origin
  TAG="$(as_op git -C "$SRC" tag -l 'v*' --sort=-v:refname | head -1)"
  [ -n "$TAG" ] || fail "no release tag in $SRC"
  as_op git -C "$SRC" checkout -q "$TAG"
  echo "  release: $TAG"
fi
VERSION="$(as_op git -C "$SRC" describe --tags --always 2>/dev/null || echo dev)"

# ── [3] Runtime layout (repo -> working directories) ────────────────────────
say "[3/7] Runtime layout under $BASE"
mkdir -p "$FC_DIR/bin" "$FC_DIR/instances" "$FC_DIR/run" "$FC_DIR/audit"
rsync -a "$SRC/manager/manager.py" "$SRC/manager/chatui.py" "$SRC/manager/webterm.py" \
         "$SRC/manager/text_unicode.py" \
         "$SRC/manager/setup-nfs-host.sh" "$SRC/manager/logo.svg" \
         "$SRC/manager/run-tests.sh" "$FC_DIR/" 2>/dev/null || true
# Seeds, not code: the catalog, personas and secret policy are the operator's
# once they exist — an update must never reset them.
rsync -a --ignore-existing "$SRC/manager/mcp-catalog.json" "$SRC/manager/personas.json" \
         "$SRC/manager/secret-policy.json" "$FC_DIR/" 2>/dev/null || true
printf '%s\n' "$VERSION" > "$FC_DIR/VERSION"       # what the Settings tab compares with the newest release
rsync -a "$SRC/manager/templates/" "$FC_DIR/templates/"
rsync -a "$SRC/manager/mgr/" "$FC_DIR/mgr/"
rsync -a "$SRC/manager/tests/" "$FC_DIR/tests/" 2>/dev/null || true
for pair in "openrouter:openrouter-agent" "claude:claude-signal-firecracker"; do
  from="${pair%%:*}"; to="${pair##*:}"
  [ -d "$SRC/agents/$from" ] && rsync -a --exclude '__pycache__' "$SRC/agents/$from/" "$BASE/$to/"
done
for d in voice embed mcp-hub; do [ -d "$SRC/$d" ] && rsync -a "$SRC/$d/" "$BASE/$d/"; done
chmod +x "$FC_DIR/run-tests.sh" 2>/dev/null || true

# ── [4] Binaries: firecracker + guest kernel ────────────────────────────────
say "[4/7] Firecracker $FC_VERSION + kernel"
if [ ! -x "$FC_DIR/bin/firecracker" ]; then
  T="$(mktemp -d)"
  curl -fsSL -o "$T/fc.tgz" \
    "https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/firecracker-${FC_VERSION}-x86_64.tgz"
  tar -xzf "$T/fc.tgz" -C "$T"
  install -m 0755 "$T"/release-*/firecracker-*-x86_64 "$FC_DIR/bin/firecracker"
  rm -rf "$T"
fi
"$FC_DIR/bin/firecracker" --version | head -1
if [ ! -f "$FC_DIR/bin/vmlinux" ]; then
  if [ -n "${VMLINUX_URL:-}" ]; then
    curl -fsSL -o "$FC_DIR/bin/vmlinux" "$VMLINUX_URL"
  elif [ -f "$SRC/manager/bin/vmlinux" ]; then
    cp "$SRC/manager/bin/vmlinux" "$FC_DIR/bin/vmlinux"
  else
    fail "guest kernel missing: set VMLINUX_URL=<release-asset-url> (or place bin/vmlinux manually into $FC_DIR/bin/)"
  fi
fi
echo "  kernel: $(du -h "$FC_DIR/bin/vmlinux" | cut -f1)"

# ── [5] Builds (Docker) ──────────────────────────────────────────────────────
if [ "$NO_BUILD" = "0" ]; then
  say "[5/7] Build rootfs + services (takes a while the first time)"
  ( cd "$BASE/openrouter-agent" && FC_DIR="$FC_DIR" bash build-openrouter-rootfs.sh )
  # iroh app<->manager gateway (Rust, built in Docker; gives the app a P2P
  # transport so it needs no VPN and no exposed HTTPS port).
  ( cd "$SRC/iroh-gw" && bash build.sh ) && install -m0755 "$SRC/dist/iroh-gw" "$FC_DIR/bin/iroh-gw"
  ( cd "$BASE/embed"   && docker build -q -t kaim56-embed .   && docker rm -f kaim56-embed 2>/dev/null; \
    docker run -d --restart unless-stopped --name kaim56-embed -p 127.0.0.1:8772:8772 kaim56-embed )
  ( cd "$BASE/mcp-hub" && docker build -q -t kaim56-mcp-hub . && docker rm -f kaim56-mcp-hub 2>/dev/null; \
    docker run -d --restart unless-stopped --name kaim56-mcp-hub -p 127.0.0.1:8771:8771 kaim56-mcp-hub )
  if [ "$WITH_VOICE" = "1" ]; then
    ( cd "$BASE/voice" && docker build -t kaim56-voice . && docker rm -f kaim56-voice 2>/dev/null; \
      docker run -d --restart unless-stopped --name kaim56-voice -p 127.0.0.1:8770:8770 kaim56-voice )
  fi
  if [ "$WITH_AGENTS" = "1" ]; then
    ( cd "$BASE/claude-signal-firecracker" && FC_DIR="$FC_DIR" PATH="$PATH:/sbin:/usr/sbin" bash build-rootfs.sh )
  fi
  if [ "$WITH_HINDSIGHT" = "1" ]; then
    # Second memory (optional). No key in the container: its LLM calls go to the
    # manager's key proxy on the docker bridge and are booked as "hindsight".
    docker pull -q ghcr.io/vectorize-io/hindsight:latest && docker rm -f kaim56-hindsight 2>/dev/null
    docker run -d --restart unless-stopped --name kaim56-hindsight \
      -p 127.0.0.1:8888:8888 -p 127.0.0.1:9999:9999 \
      -e HINDSIGHT_API_LLM_PROVIDER=openai \
      -e HINDSIGHT_API_LLM_BASE_URL=http://172.17.0.1:8700/api/llm/openrouter \
      -e HINDSIGHT_API_LLM_MODEL="${HINDSIGHT_MODEL:-google/gemini-2.5-flash}" \
      -e HINDSIGHT_API_LLM_API_KEY=proxy \
      -v hindsight-data:/home/hindsight/.pg0 ghcr.io/vectorize-io/hindsight:latest
    echo "  Hindsight on 127.0.0.1:8888 (UI :9999) — enable it in Settings: HINDSIGHT_URL=http://127.0.0.1:8888"
  fi
else
  say "[5/7] Builds skipped (--no-build)"
fi

[ "$FILES_ONLY" = "1" ] && { say "DONE (files-only): layout under $BASE is in place."; exit 0; }

# ── [6] systemd service ──────────────────────────────────────────────────────
say "[6/7] systemd service (needs sudo)"
HOSTIF="$(ip route 2>/dev/null | awk '/default/{print $5; exit}')"
# An existing unit may carry settings this installer does not know (a fixed
# MANAGER_USER/MANAGER_PASS behind a reverse proxy, LLM_KEY_PROXY, …): every
# Environment= line except the ones written below is carried over.
KEEP_ENV="$($SUDO cat /etc/systemd/system/firecracker-manager.service 2>/dev/null \
  | grep '^Environment=' | grep -vE '^Environment=(PORT|HOSTIF|GUEST_DNS|AGENT_ROOT)=' || true)"
# The password lives in a root-only env file the unit always references;
# it is generated once (an update run must not drop the login).
PASS_LINE="EnvironmentFile=-/etc/firecracker-manager.env"
if ! $SUDO test -f /etc/firecracker-manager.env && ! printf '%s' "$KEEP_ENV" | grep -q '^Environment=MANAGER_PASS='; then
  PW="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 20)"
  printf 'MANAGER_PASS=%s\n' "$PW" | $SUDO install -m 600 -o root -g root /dev/stdin /etc/firecracker-manager.env
  echo "  web login: admin / $PW   (changeable in /etc/firecracker-manager.env)"
fi
$SUDO tee /etc/systemd/system/firecracker-manager.service >/dev/null <<UNIT
[Unit]
Description=Firecracker Manager (kAIm56)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$FC_DIR
Environment=PORT=8700
Environment=HOSTIF=$HOSTIF
Environment=GUEST_DNS=$GUEST_DNS
Environment=AGENT_ROOT=$BASE/agent
$KEEP_ENV
$PASS_LINE
ExecStart=/usr/bin/python3 $FC_DIR/manager.py
Restart=on-failure
# The VMs are children of the manager: KillMode=process ends only the
# manager itself on restart/update, the agents keep running.
KillMode=process

[Install]
WantedBy=multi-user.target
UNIT
$SUDO sysctl -qw net.ipv4.ip_forward=1
echo net.ipv4.ip_forward=1 | $SUDO tee /etc/sysctl.d/99-kaim56.conf >/dev/null
# The update unit: root, oneshot, this installer again with the same options
# plus --release; the manager starts it from the Settings tab (/api/update)
# and shows its log (run/update.log).
FLAGS="--release"; [ "$WITH_VOICE" = 1 ] && FLAGS="$FLAGS --with-voice"; [ "$WITH_AGENTS" = 1 ] && FLAGS="$FLAGS --with-agents"; [ "$WITH_HINDSIGHT" = 1 ] && FLAGS="$FLAGS --with-hindsight"
$SUDO tee /etc/systemd/system/kaim56-update.service >/dev/null <<UNIT
[Unit]
Description=kAIm56 update (install.sh --release, started from the web UI)
After=network-online.target docker.service

[Service]
Type=oneshot
WorkingDirectory=$SRC
Environment=KAIM56_BASE=$BASE
Environment=KAIM56_USER=$OP_USER
Environment=GUEST_DNS=$GUEST_DNS
Environment=HOME=/root
StandardOutput=append:$FC_DIR/run/update.log
StandardError=append:$FC_DIR/run/update.log
ExecStart=/bin/sh $SRC/install.sh $FLAGS
UNIT
$SUDO systemctl daemon-reload
$SUDO systemctl enable firecracker-manager >/dev/null 2>&1 || true
# Restart, not "start": on an update the running manager would keep the old
# code and the old unit environment (it did on the deployment test VM).
$SUDO systemctl restart firecracker-manager

# iroh gateway (only if the binary was built) — the app's P2P transport.
if [ -x "$FC_DIR/bin/iroh-gw" ]; then
  $SUDO tee /etc/systemd/system/iroh-gw.service >/dev/null <<UNIT
[Unit]
Description=kAIm56 iroh gateway (app<->manager transport over iroh, P2P)
After=network-online.target firecracker-manager.service
Wants=network-online.target

[Service]
Type=simple
Environment=IROHGW_SECRET=$FC_DIR/iroh-gw/secret.key
Environment=IROHGW_ALLOW=$FC_DIR/iroh-gw/allow.txt
Environment=IROHGW_NODEID=$FC_DIR/iroh-gw/nodeid.txt
Environment=IROHGW_MANAGER=127.0.0.1:8700
ExecStart=$FC_DIR/bin/iroh-gw
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable iroh-gw >/dev/null 2>&1 || true
  $SUDO systemctl restart iroh-gw
fi

# ── [7] Smoke test ───────────────────────────────────────────────────────────
say "[6b/7] NFS server (workspace and memory folders of the VMs; needs sudo)"
$SUDO AGENT_DIR="$BASE/agent" "$FC_DIR/setup-nfs-host.sh" | tail -3

say "[7/7] Smoke test"
sleep 3
CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8700/" || echo 000)
case "$CODE" in
  200|401) echo "  manager responds (HTTP $CODE) ✓";;   # 401 = running, auth active
  *) fail "manager not responding (HTTP $CODE) — journalctl -u firecracker-manager";;
esac
FC_DIR="$FC_DIR" AGENT_PATH="$BASE/openrouter-agent/agent.py" \
  python3 "$FC_DIR/tests/e2e.py" AgentLogic ManagerFunctions 2>&1 \
  | grep -E '^(FAIL|ERROR):|^Ran |^OK|^FAILED' || true

say "DONE"
cat <<EOF

  Web UI:    http://$(hostname -I 2>/dev/null | awk '{print $1}'):8700
  Next steps:
    1. Settings tab: enter your OpenRouter or OrcaRouter API key
    2. Instances tab: create the first instance from the openrouter template
    3. optional: --with-agents for the claude template, --with-voice for STT/TTS
EOF
