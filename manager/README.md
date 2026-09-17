# firecracker — microVM platform + web manager

Manages **1..x Firecracker microVMs** through a small web UI. First instance =
the Signal↔Claude Code bridge (`claude`).

```
firecracker/
├── bin/           firecracker (v1.16.1 ✅) + vmlinux (kernel, see below)
├── instances/     <name>.json + <name>-rootfs.ext4  (per instance)
├── run/           runtime (sock/pid/log/config) – automatic
├── manager.py     web UI + API (port 8700), runs as root
├── chatui.py      chat frontend (served by the manager under /chat)
├── firecracker-manager.service   systemd autostart
└── traefik-firecracker.yml       exposure via manager.example.com
```

## Chat frontend (`/chat`)
Chatting with the agents runs through the manager's **`/chat`** page — same
origin, same Traefik auth, no extra service and no extra port. The
**💬 Chat** button in the instance table opens `/chat?i=<instance>`.

Features: history sidebar, streaming responses, Markdown with code blocks +
copy, image attachment (vision), cancel, light/dark, mobile.

| Endpoint | Meaning |
|---|---|
| `GET /chat[?i=<instance>]` | the frontend (`chatui.py`) |
| `POST /api/chat/<instance>` | `{message, image?}` → response tokens as raw text (stream) |

Details:
- The chat targets any instance with `TRANSPORT=web`; a **stopped instance starts
  on the first prompt** (waits for guest port 8080, max. 120 s).
- Streaming goes through the bridge's `/api/chat/stream` if it supports it
  (openrouter); otherwise the response comes as a single chunk.
- The **history lives in the browser's localStorage** — the microVM keeps its
  own session (`claude --resume` or `_history`), so only the new message goes
  out per turn. Starting a new chat does *not* start a new agent session;
  for that there is 🔄 at the top of the page (stops + starts the instance).
- The old chat pages of the bridges remain reachable under `/i/<name>/`
  (fallback; the agent-to-agent API `/i/<name>/api/chat` uses the same path).

## Network model (per instance from `index`)
`host 172.30.<index>.1/30` · `guest 172.30.<index>.2/30` · `tap fc<index>` · MASQUERADE via `eth0`, guest DNS Pi-hole.

## Instance JSON (`instances/<name>.json`)
```json
{ "name":"claude", "index":1, "vcpus":2, "mem_mib":1536,
  "rootfs":"instances/claude-rootfs.ext4",
  "extra_drives":[ {"path":"/path/data.ext4","readonly":false} ] }
```
New instance = new JSON with a different `index` + its own `rootfs.ext4`.

## Getting host folders into the VM
Firecracker **cannot** bind-mount host folders (only block devices + network):
- **Extra disk:** attach an ext4 image as `extra_drives` (snapshot or disk that only the guest writes to).
- **Live share:** mount an **NFS/SMB share** inside the guest (a real live folder) — the clean way when Claude needs a host folder. For that, export NFS on the host and add `mount -t nfs …` to the instance's `guest-init.sh`.

You no longer type host paths by hand: **📁** in the mount row (both when creating and in the
instance table) opens a **folder browser** via `GET /api/browse?path=…`
(directory names only, admin-only, hidden folders excluded — typing directly
still works). The guest path is suggested as `/mnt/<foldername>`.

## Policy & audit (tab "Policy")
One view per instance: what it **may** do (internet toggle, editable tool
allowlist, allowed secrets, MCP, model) and what it **does** — the
*Activity* button shows the most recently invoked tools and targets (URL/path/query) from
the audit log. The agent reports every tool call to `/api/audit`; it is stored
as `audit/<name>.jsonl` on the host (survives restarts, without secret values).
`GET /api/policy`, `GET /api/audit/<name>` — admin-only.

## Tasks (tab "Tasks")
Scheduled work per instance: a **job** (the message to the agent) runs
once or recurring. Schedule formats: `every Nm|Nh|Nd`, `daily HH:MM`,
`hourly`; empty = once immediately. A background worker in the manager runs due
tasks (starting the instance if needed) and reschedules recurring ones.
`GET/POST /api/tasks`, `POST /api/tasks/<id>/delete` — admin-only.

## Capabilities per instance
On creation: **internet access** (on/off) and a **tool allowlist**
(checkboxes). Internet off = the VM only reaches the manager broker, not LAN/web
and therefore not the LLM. Toggleable live in the table (🌐/🚫). The tool selection
lands as `AGENT_TOOLS` in the config; the agent filters schema and execution.
`bash` is the master key — to truly lock things down, deselect `bash` as well.

**Delegation** (`DELEGATE_TARGETS` in the instance config): a guest may create
tasks (`create_task`) only for **itself** or an **ephemeral** VM. Other instances
must be listed — comma-separated, `*` = all; the `orchestrator` may address
everything. The same list scopes what `list_agents` shows the guest. Evaluated by
the manager at request time, so it applies **without** a restart. Without it a
prompt-injected agent could run its text on any instance — with that instance's
secrets and MCPs.

## Editor link (VS Code in the footer, plugin files)
`CODE_URL` in `site.json` puts a **VS Code** link into the footer (code-server or
openvscode on the host; a link, not an iframe — the manager is HTTPS, the editor
usually plain HTTP in the LAN). With `CODE_ROOT` — the path under which the
manager directory is mounted **inside** the editor — every file name in the
Plugins tab opens that file directly in VS Code (`?folder=…&payload=[["openFile",
"vscode-remote://host:port/…"]]`), so a plugin is read where it is edited. The
reference setup runs code-server in Docker with both trees mounted:

```bash
docker run -d --name kaim56-code --restart unless-stopped -u 1000 \
  -p 192.168.0.10:8443:8080 -e PASSWORD=… \
  -v /home/ulrich/kaim56:/home/coder/kaim56 \
  -v /home/ulrich/firecracker:/home/coder/firecracker \
  -v ~/.config/code-server-data:/home/coder/.config \
  -v ~/.local/share/code-server-data:/home/coder/.local/share/code-server \
  codercom/code-server:latest --bind-addr 0.0.0.0:8080 --auth password /home/coder/kaim56
```

## Guest boundary (what a VM may reach)
A guest is recognized by its **source IP** (`instance_by_ip`); a per-tap
iptables rule drops packets with any other source, so the IP is a usable
identity. Guests carry no credentials: `_auth` exempts them, admins need Basic
auth when `MANAGER_PASS` is set. On the HTTP side:

- **POST** only for the paths in `GUEST_POST_PATHS` / `GUEST_POST_PREFIXES`
  (403 otherwise).
- **GET** denied for `/`, `/chat`, `/katfs`, `/katfs/…` and `/i/<name>/…`
  (`GUEST_GET_DENIED_*`) — the admin UI, the web chat, the katfs browser and the
  per-instance proxy including the WebSocket terminal.
- **Scoped reads:** `/api/agents` lists what the guest may delegate to,
  `/api/history` returns only runs it created or executed, `/api/hitl/<id>`
  answers only the requesting instance, `/api/inbox` is the orchestrator's alone.
  Memory, playbooks, missions, usage and audit are pinned to the caller's own
  instance already.

On the host, `ensure_guest_input_rules()` limits guest→host traffic on the
`fc+` taps to the manager port (8700) and NFS (2049) plus ICMP and established
replies — every other host service (sshd, rpcbind, …) is unreachable from a VM.

## Model selection (tab "Models")
The tab pulls the **full OpenRouter catalog live** (~400 models, cached for
10 min, *Refresh catalog* bypasses the cache) and lets you tick the **shortlist**
from it that appears in the model dropdown when creating an instance. It is saved
in `models.json`; if the file is missing, the `CURATED` constant in
`manager.py` provides the initial fill. Saving takes effect immediately — no restart.

Filters: text search over id and name, *tool calling only* (default) and
*selected only*. **Note:** The openrouter template pulls the list with
`tools=1`; a model without tool calling therefore stays invisible, even if it
is ticked — which is why the column indicates it.

| Endpoint | Meaning |
|---|---|
| `GET /api/openrouter-models[?refresh=1&tools=1&relevant=1]` | catalog (`relevant=1` = shortlist only) |
| `GET /api/models` | the saved shortlist |
| `POST /api/models` | save `{curated:[id,…]}` |

## katfs — folders from the browser (tab "Sharing")
Not a host folder, but the directory **of the machine you're sitting at right now**:
`iroh-fs/` shares it via P2P with the agents (`remote_ls` / `remote_read` / `remote_write`).
The manager passes the sharing page through under **`/katfs/`** — same origin,
same auth and therefore **HTTPS**, which the File System Access API strictly requires;
the SSH tunnel or the dedicated `katfs.example.com` route from `iroh-fs/README.md` is
no longer needed for that. The tab shows node status and whether a browser is currently sharing
(`GET /api/katfs/status`). **No mount:** close the tab and the folder is gone —
for permanent folders use the NFS mounts above.

**Sharing key** = the **node-id** of the node the browser connects to
(iroh parses it as `EndpointId`: 64 hex characters — the WASM bridge does *not*
accept a *ticket*, despite the placeholder text on the page). It is shown in the tab
for copying and is editable: *Share a folder…* then opens
`/katfs/?key=<node-id>`, and the proxy sets exactly this value in the
`#nodeid` field of the sharing page. This lets the same browser deliver a folder to
a **foreign** katfs node as well; *Reset* brings back its own node-id.

**Sharing works two ways.** In the **browser** only with Chromium/Edge over
HTTPS — Firefox and Safari have no API that writes into a real user folder.
Anyone who needs read/write (or uses Firefox) instead uses
`iroh-fs/dist/katfs-share <node-id> <folder>`: the same provider as a native
program, without a browser, with write access and a stable share-id.

**Multiple shares, selection on creation.** The node holds any number of
shares at once; each announces a **share-id** on connect (in the browser
from `localStorage`, for `katfs-share` derived from hostname + path) plus
folder name, platform and whether it is read-only.
The Sharing tab lists them, and the creation form has **katfs share** as a
selection → the chosen value lands as `KATFS_SHARE` in the instance config, and the
agent tools append it as `&share=…`. Without a selection, the node serves the
request only as long as **exactly one** share is active — with several it names
the ids instead of guessing.

The **node-id** is something different and is **not** a per-instance value: the agent
derives its node from its own IP (`_katfs_base()` in `openrouter-agent/agent/tools_manager.py`
→ `http://<gateway>:8790`); the node-id only tells the sharing *browser* where
to connect. If an instance should use a *different* node, it needs
an address: `KATFS_URL` in the instance config (e.g. `http://10.0.0.10:8790`).

⚠️ `KATFS_SHARE`/`KATFS_URL` is only evaluated by a **freshly built openrouter rootfs**
(the `agent/` package is baked into the image). The node itself is built with
`iroh-fs/node/build.sh` (Docker, no local Rust needed).

## Agent folder (NFS live share)
`/home/ulrich/agent` is mounted live via **NFSv4** into the `claude` VM as `/root/workspace`
— Claude works there, the files live on the host. Setup (root):
```
sudo /home/ulrich/firecracker/setup-nfs-host.sh
```
On boot the guest automatically mounts `<gateway>:/ -> /root/workspace`
(controlled via `AGENT_NFS`/`AGENT_EXPORT` in `claude-signal-firecracker/config.env`).
Guest writes appear on the host as `ulrich` (all_squash/anonuid=1000).

## Setup
**0) NFS agent share** (see above): `sudo ./setup-nfs-host.sh`

**1) Get the kernel** — already done (`bin/vmlinux` = 6.1.128, 40 MB). If a new one is needed:
```
! curl -fsSL "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.12/x86_64/vmlinux-6.1.128" -o /home/ulrich/firecracker/bin/vmlinux
```
**2) Build the Claude rootfs** (docker build → you) and place it:
```
! cd /home/ulrich/claude-signal-firecracker && ./build.sh && cp rootfs.ext4 /home/ulrich/firecracker/instances/claude-rootfs.ext4
```
**3) Start the manager** (root):
```
sudo cp /home/ulrich/firecracker/firecracker-manager.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now firecracker-manager
```
→ UI reachable at `http://10.0.0.10:8700` (on the LAN) and via Traefik at
`https://manager.example.com` (after installing `traefik-firecracker.yml`).

## Exposure (manager.example.com)
Place `traefik-firecracker.yml` in the **file provider folder** of the Traefik stack
(inside the traefik container `/etc/traefik/...`). Generate the basic-auth hash with
`htpasswd -nbB admin 'PW'`. **Important:** The manager controls VMs as root —
**never expose it without auth** (basic auth or your pocket-id/SSO in front).

## Changelog & security (tab "Changelog")
The tab shows the **open findings** from `security.json` at the top — sorted by
severity, with location, description and suggested fix — and below that the rendered
`CHANGELOG.md`. Resolved items are hidden and can be revealed.

Through the UI **only the status** can be toggled (open/done); text and
assessment live in the file, so a finding can't disappear on a click.
Both routes are admin-only — the list describes holes, guests do not read
along.

| Endpoint | Meaning |
|---|---|
| `GET /api/changelog` | `CHANGELOG.md` as text |
| `GET /api/security` | `{issues:[…]}` |
| `POST /api/security` | `{issues:[{id,status}]}` — the status only |

## Credentials (secret broker)
Access credentials are **not** in the instance config and therefore not on the
config disk of the microVM. `SECRET_PARAMS` in `manager.py`
(`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) is discarded on
creation and a second time when building the config disk — older instance JSONs
are cleaned by the manager itself at startup (`migrate_secrets_out_of_instances`).

The agent fetches the key at runtime instead: `ensure_or_key()` requests
`GET /api/secret/OPENROUTER_API_KEY` from the manager. It recognizes the guest by
its **source IP** (`instance_by_ip`), checks the allowlist from
`secret-policy.json` (default deny, `by_template` ∪ `by_instance`) and only then
delivers the value. Calls that belong to no instance — e.g. from the host —
get `403 not allowed`.

The source of the values is `secret_store()`: the secret store
(`~/.config/kat56/secrets.env`, 0600) and, in addition, the manager settings, where the
LLM keys are maintained in the **Settings** tab (`settings.json`, now 0600).

**MCP servers likewise.** Previously `MCP_CONFIG` held the finished configuration
*with the tokens inserted* — in `hass.json` the HA bearer token, which therefore also
lay on the config disk. Now only `MCP_SERVERS=<name>,<name>` is stored; the
agent fetches `GET /api/mcp-config` on startup and gets the configuration with the
values that **this instance's** policy releases. Anything not released stays as
`${PLACEHOLDER}` and is reported as `unresolved`, instead of failing
silently. The endpoint answers requests only from within an instance — from the host it
returns `403 guests only`.

The manager migrates legacy state at startup itself: `migrate_mcp_config_out_of_instances`
reads the server names from the old blob, writes `MCP_SERVERS` and enters the
required secrets **per instance** into `by_instance` (i.e. `hass -> HA_TOKEN`,
not for all openrouter agents).

Consequence for operations: without an entry in `secret-policy.json`, an
openrouter agent does start, but reports `FATAL: OPENROUTER_API_KEY missing … from
secret broker` and cannot answer. An MCP server without a released token
comes up, but without access. The **Secrets** tab is where you release
these.

## Security
- Manager = root service that starts/stops microVMs and sets tap/iptables.
- Public only **with auth** (Traefik basic auth/SSO). On the LAN, limit port 8700 via firewall if needed.
- Every microVM is HW-isolated; host access only via explicitly attached disks/NFS.

## Limits
`bin/vmlinux` + `instances/*-rootfs.ext4` must be present, otherwise the start
fails (log in `run/<name>.log`). The manager itself needs no rootfs.
