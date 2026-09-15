# Changelog

Terse by design: one line per change, newest first. The narrative behind each
entry (root causes, measurements, alternatives considered) lives in git
history — `git log -p -- manager/CHANGELOG.md` before 2026-09-08 — and in the
commit messages.

## 2026-09-15
- Hindsight (vectorize.io) as an optional second memory: `mgr/hindsight.py`, container `kaim56-hindsight` (`install.sh --with-hindsight`, LLM through the key proxy from the docker bridge, booked as instance "hindsight"), on when `HINDSIGHT_URL` is set in Settings; chat turns and `memory_store` notes are retained per instance bank, recall hits merge into `/api/memory-search`, new tool `memory_reflect` → `POST /api/memory-reflect`; session panel shows it under Memory
- Auto-reset per instance: `AUTO_RESET_MIN` (template param, 0 = never) drops the conversation when the last turn is older than that many minutes — `voicecommand` paid up to 20k tokens per "radio on" for the day's history; set to 30 there; the claude bridge does the same with its session
- Claude instances: the bridge pulls the host's OAuth login before each turn when the host has a newer one and once by force on "Failed to authenticate" — the boot-time copy went stale as the host rotated its refresh token (`claudy`: "OAuth session expired and could not be refreshed"); the rootfs build no longer bakes a credential into the image; the session panel shows how long the host login is valid (needs a claude rootfs rebuild + instance restart)
- Agent: an error reply of the non-streaming LLM call carried no `role`; appended to the history it made llama.cpp reject every following turn ("Missing 'role' in message") until a reset
- Local models, streaming: a stream that ends with nothing (llama.cpp sent the headers, then died on the image) is reported as a dropped connection instead of „(empty reply)“; images leave the history, the failed call is booked
- Local models: when llama.cpp drops the connection (it crashed on every image input on the `uncensored` model server, then reloaded the model), the agent answers with one line, strips the images from the history so the next turn does not repeat the crash, and does not retry; a 503 while the model loads is reported the same way
- Local models: LLM call timeout 600 s instead of 120/180 s (`LLM_TIMEOUT`, `LLM_STREAM_TIMEOUT`) and no retry after a timeout — an image question to the CPU-hosted 27B model timed out at 180 s while the model was still processing the prompt, and the retries queued behind it
- Local models: the agent's usage report counts again (calls, tokens, spans in the traces) — with the key proxy on, guest reports were ignored across the board, so a llama instance showed zero calls forever; accepted now when the report is flagged `direct` and the instance has `LLAMA_ENDPOINT`; the stream asks llama.cpp for token counts in the last chunk
- Local models (llama backend): mid-conversation system notes ([Memory], [Playbooks], date line, deadline note) are folded into the first system message on the wire — Qwen3's chat template in llama.cpp answered HTTP 500 "System message must be at the beginning" to every turn of the `uncensored` instance since its start; `LLM_FOLD_SYSTEM=1/0` overrides; the misleading "FATAL: OPENROUTER_API_KEY fehlt" line no longer appears for a local model

## 2026-09-14
- install.sh reruns keep the operator's state: `mcp-catalog.json`, `personas.json`, `secret-policy.json` are seeded only when missing; unknown `Environment=` lines of an existing unit (fixed login, proxy flags) are carried over and no password is generated when the unit already sets one
- Updates: install.sh writes `VERSION` and installs `kaim56-update.service` (root oneshot: `install.sh --release`, newest tag, same options); the manager compares with the newest GitHub release (`GET /api/version`, cached 6 h, `UPDATE_CHECK=0` disables, `UPDATE_REPO` in site.json), footer badge and a card in Settings with an Update button (`POST /api/update`) that streams `run/update.log`; the installer runs as root from the unit (sudo no-op, git as the operator)

## 2026-09-13
- Buttons: a stray declaration left behind by the corner-mark removal (2026-09-12) made the browser drop the whole `.btn` rule — link buttons (Terminal, Chat) rendered as underlined links and every button lost its base padding and font; fragment removed
- GitHub Action `apk.yml`: native iroh module + KatAgent APK on every `v*` tag, attached to the release when the stable keystore secret is set; repo `app/` is now one buildable Gradle root (the flattened duplicate `app/src` is gone) mirrored from the live project
- Manager tab row: no visible scrollbars any more (the thin horizontal one and a stray vertical one from the tab underline); the row still scrolls by wheel/touch and keeps the active tab in view
- README: a detailed feature set before the licence (runtime, slash commands, tools, memory, autonomy, traces, security, clients)
- `docs/proxmox.md`: running kAIm56 in a Proxmox VE VM (nested virtualization, CPU type host, sizing, cloud-image `qm` recipe, firewall, snapshots); linked from Getting started
- Resources chart also loads when the page opens directly on `#resources` (only the tab switch loaded it); three unquoted SVG attributes (`rx=1/>`) quoted; README gets six screenshots (chat, resources, policy, MCP, skills, personas), anonymised like the first
- Repository moved to the `octonion-ai` organisation: `github.com/octonion-ai/kaim56` (installer default, README links); the old path redirects
- Resources tab: tokens in and out per instance (separate bars, each stacked by model on its own scale) plus cost and calls per instance (24h/7d/30d/all), legend with per-model totals; `GET /api/usage-by-model?since=`; one-shot task VMs are summed as „tasks“
- Chat: choosing another instance in the dropdown opens that instance's newest chat (or an empty one) instead of re-pointing the open chat at it
- Chat session panel drops the „Commands“ cell (the / picker already lists every command)

## 2026-09-12
- Secrets tab manages the values too: add, replace and delete keys of the host store (`~/.config/kat56/secrets.env`, written 0600, never echoed back); LLM keys stay in Settings; store path derives from the tree
- Voice: default TTS voice is `de-thorsten-high` (medium stays installed, switch in Settings); the Parakeet weights are baked into the voice image so a recreated container answers at once instead of downloading for a minute; `onnx-asr` pinned to 0.12.0
- Registration corner marks removed from every card and box (chat, composer, manager tabs)
- Tool status lines („🔧 web_search …“) sit in a collapsed „Tools · n“ block under the answer, in the app (5.35) and the web chat; the answer text no longer carries them (and Markdown no longer italicises their underscores)
- Web chat: session panel (runtime, uptime, login, MCP servers with secret state, platform services, Restart/Logs/Terminal) and inline search over the open chat (Enter/Shift+Enter walk the hits, Esc closes, Ctrl+F opens), from the claude.ai/design project „Agent Chat Ideas“; `GET /api/session/<instance>[/log]`
- Skills from experience: after a long successful turn the agent distills a SKILL proposal (one extra model call, `SKILL_LEARN=0` disables, `SKILL_LEARN_MIN_STEPS`), plus a `propose_skill` tool; proposals are linted, announced and wait in the Skills tab for approval
- `search_sessions`: full-text search (SQLite FTS5) over chats and task results, scoped to the guest's own instance, orchestrator sees all; `POST /api/sessions-search`
- Tasks tab: a play button queues a task for the next worker tick (scheduled ones keep their schedule, finished one-offs run again)
- Deployment test on a clean Debian 12 VM (nested KVM) passes end to end; fixes on the way: installer restarts services on update and keeps the login, `KillMode=process` in the unit, guest firewall rules by position (NFS ACCEPT above the DROP), instance JSONs 0640 for the operator
- Fresh-install fixes from the QEMU deployment test: `text_unicode.py` deployed, NFS set up by the installer (per-instance model, old root export retired), no operator paths in defaults (`AGENT_ROOT`, credentials, test runner, build scripts), neutral `secret-policy.json` in the repo, caldav test self-contained, smoke step shows failing tests

## 2026-09-11
- Traces in the app: the bridge names each turn in an `X-Kaim-Turn` header, the instance proxy forwards it, KatAgent 5.34 stores it on the message and opens the span tree (LLM and tool calls with durations) behind „⟲ trace“

## 2026-09-09
- Traces: one span tree per turn (turn → LLM calls → tool calls, each with duration) stitched from the audit, the usage and a new `/api/trace` marker; `GET /api/trace/<instance>?turn=` returns it, the Activity dialog groups audit lines by turn; `llm_usage` gains turn/ms/step/ok/err, new `turns` table (30-day retention)
- Task timeouts: the manager hands each turn a deadline, the agent stops its tool loop in time and answers with a partial result; worker-run tasks get 30 min (`TASK_TIMEOUT`); `/steps N <text>` (alias `/maxSteps`) caps one turn — a job search ran past the old 10-minute limit and its result was lost
- Audit noise: the empty-notify check runs offline now; as an HTTP test it left an `admin/notify empty` audit line on every suite run (138 of them read like a misbehaving admin instance)
- NFS per instance: the workspace `agent/<name>` and every host folder are exported to that VM's address only (no pool-wide root export, no crossmnt); guests mount by absolute path, `AGENT_EXPORT` on the config disk
- Guest writes are squashed to a dedicated system user `kaim56-guest` (created by the manager) instead of the operator's uid; workspaces and memory folders belong to it, group = operator
- State files (chats, missions, tasks, history, audit) are root-only (umask 077 + one-time chmod); ten failed logins lock the client for 15 minutes

## 2026-09-08
- Security review (skills, fresh-context subagent): request bodies capped (413), CSRF Origin check on admin POSTs, constant-time Basic auth, `nosniff`/`SAMEORIGIN` headers, socket timeout
- Guest boundary: the reconciler fetches its mount list from `/api/mounts` (by IP) instead of the shared workspace; host/guest mount paths validated (no manager tree, `~/.ssh`, `/bin`…); forged pool addresses on the LAN interface dropped
- Terminal tunnel forwards only upgrade headers (no Authorization/Cookie into a VM); web-chat Markdown escapes quotes in links; UI JSON blobs script-safe
- `/api/chat-log` only from Signal-transport guests, rate-limited, inbox marks relayed messages; LLM proxy books upstream usage (budget no longer self-reported); at most 2 ephemeral VMs at once
- `/api/resources` admin-only, web search metered per guest; installer keeps the manager password in a 0600 env file
- Memory as files: `mgr/memfs.py` keeps one Markdown folder per instance (notes, daily timeline coarsened to weekly, git-committed), mounted read-write at `/memory`; the agent gets its index every turn
- Harness disk: the agent code (`AGENT_SRC`) rides a read-only 8 MB ext4 drive rebuilt on content change, so an agent fix is one instance restart instead of a rootfs build; stale badge counts it
- Secrets: a release lets the hub substitute on the host, only keys in `guest_readable` reach a VM raw (Secrets tab column); existing installs are seeded from their releases once
- Guest DNS comes from `site.json` via the config disk (`GUEST_DNS`), no address in the image; one `mkfs_image` helper for config, upper and harness drives
- if-chain gone: every HTTP path is a registered route, `_do_GET`/`_do_POST` dispatch only; 104 routes, test guards the chain stays empty
- Route handlers share `_body`/`_json`/`_guest` helpers and one error decorator; unknown API paths answer 404 instead of the admin page
- Architecture tab: client row reordered (Desktop client first), iroh/Traefik arrows re-routed; README screenshot regenerated
- Git history rewritten (git-filter-repo): phone numbers, LAN prefix, private host, `mcp-catalog.json`, author e-mails scrubbed
- GitHub is `origin` with full history; `push-github.sh` squash publisher removed; Gitea kept as private archive remote `gitea`
- Agent appends playbooks, recall notes and missions right before the current message instead of at the top of the context
- voicecommand "Uhr" playbook rewritten as a positive rule with examples — the negative wording was ignored by the model
- caldav-mcp: `patch-caldav.js` normalizes `YYYY-MM-DD HH:MM zone` and bare dates into ISO with offset for `start`/`end`/`due`/`until`
- `[Now]` line names the UTC offset and the exact tool-input datetime form; voicecommand playbook: default calendar, 1 h default, never ask
- `ha_control`: area name + group cue ("licht", "alle") beats a good fuzzy hit (≥0.9 still wins) — relay was switched instead of lights
- `ha_control`: `learn_alias` no longer crashes on a `None` alias in HA's registry
- `/api/tts` filters every text with `speakable_text()` (tool lines, think blocks, code, URLs) — Read aloud spoke "🔧 ha_control …"

## 2026-09-07
- `espclient/` (MrVoice, push-to-talk ESP32-S3 client, ESP-IDF/PlatformIO) joins the monorepo; credentials via NVS/`config_local.h`
- Architecture tab gains an "ESP32 client (MrVoice)" card
- Voice turns via `/api/chat/<inst>` mirrored into the shared chat store as `Voice · <inst> · <time>` (`voice_session()`); `/reset` archives
- `/api/stt-recent/audio?i=N` (admin) returns the N-th most recent STT upload (last 5, memory only) — diagnosed the ESP32's flat-line mic
- `[Now]` line moved from context top to right before the current message with "earlier times are outdated" — gemini-flash reused stale times
- `GET /api/stt-recent` (admin): last 50 STT transcripts with time, seconds and caller, memory only
- Hub processes get `TZ` = host zone via `mgr/mcp.py: hub_env()` (catalog may override) — ts-caldav parsed Berlin times as UTC
- caldav patches moved to `mcp-hub/patch-caldav.js`; `list-events` returns `YYYY-MM-DD HH:MM <zone>`, whole-day events as plain dates
- `[Now]` line tells the model to convert `…Z` timestamps before stating a time; known limit: recurring events report master DTSTART
- mcp-hub image patches caldav-mcp `list-calendars` to absolute URLs against `CALDAV_BASE_URL` — relative hrefs broke `list-events`
- Agent injects one `[Now]` system line per turn (weekday, date, time, zone); manager writes `TZ` (`GUEST_TZ` override) into every config disk
- `--smoke` and the live test wait up to 120 s for the agent bridge before asking `/tools`
- Policy tab: MCP servers of an existing instance as checkboxes; Save MCP writes `MCP_SERVERS` via the config route, offers Restart now
- Each MCP server shows its `${SECRET}` placeholders as released/unreleased; route refuses names not in the catalog (`mcp_servers_error()`)
- Stale-image detection: `image_state()` compares VM start with image build time; `/api/instances` carries `stale`, tab shows "old image"
- New `restart` action (stop + start) and "Restart on new image" button; idle worker `image_sweep()` pushes once per rebuild
- `build-openrouter-rootfs.sh --smoke <instance>` restarts that instance after the build and runs the model-free `/tools` registry test
- Agent `_resolve_tool_name()` accepts a bare MCP tool name when it matches exactly one registered tool — model dropped the `server__` prefix
- New `/tools` command lists the registry as the model sees it, without a model call
- `LiveAgent.test_tools_registry_after_rebuild` smoke-tests `/tools` on every instance started after the current rootfs build
- `spawn_subagent(task, model=)` rebuilt on `create_task(target=ephemeral, wait=true)` — the old `/i/<name>/` path had 403'd since 08-14
- `create_task` and `/api/task` accept `model`; carried into `_run_ephemeral` (`OPENROUTER_MODEL`) for waited and queued runs

## 2026-09-06
- Architecture tab: "Fedora voice client" renamed "Desktop client" (text only)
- `resolve_task_target()` strips a leading `@` and refuses unknown names at task create/edit — MSFT task silently failed on `@orchestrator`
- Worker pushes "Scheduled task failed: <id>" once per distinct failure text; hourly `task_target_sweep()` pushes "Task target unknown" once
- Tasks editor can move a task to another instance (`update_task(..., instance=)`); MSFT task retargeted and reworded to fetch, not schedule
- Web UI dead: a `'\n'` Python escape in `mgr/ui_js.py` broke the whole client script; fixed to `'\\n'`
- `tests/check_js.py` + `run-tests.sh` step 0d run `node --check` on the rendered client script (host node or the mcp-hub container)
- Plugins tab: file names deep-link into code-server (`site.json` key `CODE_ROOT`); in-manager viewer and `/api/plugins/<name>/file` removed
- code-server container needs the live manager tree mounted (command in `manager/README.md`)
- Missions tab: Edit (goal, steps by position, status incl. reopen) and Delete for any status via `/api/mission-admin`; all finished listed
- Plugins tab: read-only source viewer `GET /api/plugins/<name>/file` (admin, path-confined, 512 KB) so approval is not blind (superseded)
- Guest GET denylist `guest_get_blocked`: `/`, `/chat`, `/katfs…`, `/i/<name>/…` — a VM could open every other VM's terminal
- `/api/task` target policy `guest_may_target`: self or `ephemeral` only, others via `DELEGATE_TARGETS` (comma list, `*`); orchestrator keeps all
- `/api/agents`, `/api/history`, `/api/hitl/<id>` scoped per guest; `/api/inbox` orchestrator-only among guests
- Host firewall: `ensure_guest_input_rules()` limits guest→host to :8700 and :2049; `ensure_antispoof()` per tap drops foreign source IPs
- `_auth` exempts guests (source IP + allow/deny lists) — a set `MANAGER_PASS` would have locked every agent out
- CalDAV as MCP server: `caldav-mcp` (11 tools, events + VTODO) installed in the `mcp-hub` image; catalog entry `caldav` configured by env
- `CALDAV_PASSWORD` as `${CALDAV_PASSWORD}` from the host secret store; released for `myassistant`/`voicecommand`; values still placeholders

## 2026-09-04
- Architecture SVG: Halo glasses (off the app) and Fedora/Linux voice client added; `iroh-gw` beside Traefik; two new component cards
- `voicecommand` instance got its secret releases (HA/mrmusic/portainer) — Home Assistant had loaded 0 tools
- New agent tool `ha_control(spoken, action)`: deterministic matching in the manager (exact → strong fuzzy → area + group cue → weak fuzzy)
- `ha_control` auto-learns the spoken phrase as an HA alias on fuzzy hits; HA token never leaves the host
- `mgr/haalias.py` (HA WebSocket registry + REST), guest routes `/api/ha-alias` + `/api/ha-control`; "Gartenhaus" HA area created
- Voice client: `"prompt"` config / `--prompt` prepends a `[Voice-Client] …` frame to every spoken message (default: short, speakable)
- Voice client speaks sentence-streamed (splitter on the token stream, think/code held); `--headless` prints stage timings
- mrmusic MCP registered (`mcp-remote` → `https://mrmusic.kat56.de/mcp`, `${MRMUSIC_TOKEN}` host-side); released for myassistant, 7 tools

## 2026-09-03
- Heartbeat skip reschedule was never saved → same heartbeat re-skipped every 5 s; `worker_claim()` now has an explicit dirty contract
- Voice client local wake word (`"wake_mode": "local"`): `--enroll` records MFCC templates, subsequence DTW gate, pure Go; `--wake-test`
- On a local wake hit the word is cut from the audio and only the remainder goes to STT — so "Kaim" works despite STT mangling
- Wake-word fixes: channel mean in the model, enrollment takes trimmed to voiced core, distance uses c1..c12
- Wake word takes a comma-list of variants (default `"Kati, Katharina"`), Levenshtein 1 for 4+ letters; tray shows dropped utterances as ✕
- Voice client and tunnel are one binary: release build embeds `kaim56-tunnel` via `go:embed`, materialized under `~/.cache/kaim56-voice/`
- Voice client `"iroh": "<node-id>"` config: client spawns `kaim56-tunnel` itself (Pdeathsig), no `base_url`/`user`/`pass` needed
- Voice client refuses a config still carrying the `manager.example` placeholder at startup
- Voice client `"wake_word"` (default `"Kat"`): only transcripts starting with the word reach the agent; word alone answers "Ja?"
- New `kaim56-tunnel` (third binary in `iroh-gw` crate): 127.0.0.1:8701 listener splices TCP onto iroh streams; `--id` prints the NodeId

## 2026-09-02
- Voice client rewritten in Go: one static binary, tray via `fyne.io/systray`, same config file and VAD; `--probe "text"` runs the whole chain
- `run-tests.sh` step 0c runs `go vet` + `go test` (skipped without a Go toolchain)
- `voice-client/`: single-file Python tray client (GNOME/KDE), energy VAD, `/api/stt` → `/api/chat/<inst>` → `/api/tts`; mic muted meanwhile
- VAD noise floor learns only from unvoiced frames — loud speech dragged the threshold past its own RMS
- Agent prompt: store the essence of attached documents in the same turn; "where knowledge goes" rule (memory/playbook/skill/recall_tasks)
- Memory writes support deletion (`value: null`); e2e memory test cleans up; saddler ignores `e2e-*` instances
- Saddler reports usage and cost per instance (both windows, from `llm_usage`)
- Idle heartbeats no longer wake the model: worker reschedules the scheduled heartbeat when inbox is empty and no mission is active
- RMW races closed: gateway toggle via `with_gateway()`, one lock across both `chats.json` cycles; dead second lock in gateway.py removed
- Four more GET routes in the routing table (prompts, resources, iroh status, voice-health): 17 enumerable
- `mgr/ui.py` split into css / html (`ARCH_SVG` its own constant) / js, assembled to a byte-identical `PAGE`
- katfs web client published as a standalone site at `https://uneidel.github.io/katfs-web/`; page reads `?key=`/`#` client-side
- Python review (`python-code-quality`, `python-security-audit`): 14 of 56 silent excepts now log where silence hid damage
- `/api/browse` bounded to `BROWSE_ROOTS` (default /home, /srv, /mnt, /media; site.json override) — it enumerated the whole filesystem as root
- `configure()` injection contracts typed across all mgr modules
- ruff (pyflakes rules) is step 0b of `run-tests.sh`, optional; first run found 10 issues, all fixed
- wdm0006/python-skills (MIT): 31 skills imported into `.claude/skills/` as dev skills, prefixed `python-`/`rust-`; agent catalog back to 67
- Task store RMW race closed: `with_tasks(mutator)` is the only way to change the store; all ten call sites converted; five-thread test
- `tests/check_names.py` (undefined-name gate, stdlib AST) is step 0 of `run-tests.sh` and a pre-commit hook
- Gate found four dormant defects: `save_gateway` import, `base64` in `mgr/gateway.py`, both secret functions never injected in `mgr/mcp.py`
- Task creation was broken for 13 days (missing `uuid` import in `store.py` since the mgr/ split); `test_add_task_roundtrip` added
- Notify route audits the failure reason (`empty` / `rate limit`) — 38 "failures" were the e2e suite; Activity badge says `failed`
- Activity panel shows error text under failed calls and a result peek for healthy ones
- Audit records tool calls AFTER execution: real `ok`, error text, result excerpt, per-turn id (additive fields)
- `mgr/saddler.py` + `GET /api/saddler`: weekly failure digest grouped by error shape, this week vs last; orchestrator-only among guests
- Weekly scheduled task hands the digest to the orchestrator to diagnose and propose playbook lines; patches stay a human decision

## 2026-09-01
- Claude guest: `kaim56_mcp.py` stdio MCP server exposes memory_store/recall, `web_search`, `list_skills`, `load_skill`, `notify` as tools
- `/branch` renamed `/aside` across agent, web chat, app (v5.32) and Claude bridge; `/branch` stays a silent alias
- Claude guest-init writes a "kAIm56 platform" section (memory, websearch, skills, notify recipes) into the workspace `CLAUDE.md`
- Test fixture: `known` backend set includes `claude`
- Claude web bridge intercepts platform slashes: `/fresh`, `/model sonnet|opus|haiku|<id>|default`; other built-ins answer "OpenRouter only"
- Web chat branch button is an in-house inline SVG (no proprietary icon); app v5.31 slash picker adds `/fresh`, `/branch`, `/back`
- System prompt states where tools execute (the agent's own microVM, not the provider) and to retry instead of concluding "blocked"
- `http_fetch` sends a browser user agent plus Accept-Language — portals 403 obvious bot agents
- `memory_recall` URL-quotes the key, manager URL-decodes path segments, slashes stay in the key — a key with a space could never be recalled
- `http_fetch` converts HTML to readable text with link targets in brackets; `raw=true` keeps the body; read cap 5 MB
- Tool schemas: `web_search` says Brave-backed, `http_fetch` documents text conversion and `raw`
- `GET /api/websearch` + `mgr/websearch.py`: search served by the manager; Brave (`BRAVE_API_KEY`) → DDG HTML (ads filtered) → Bing
- `web_search` multi-backend: DDG bot challenge detected and named, Bing fallback; a dead backend is a spoken error, not "no results"
- `_pdf_text` falls back to `pdftotext` in the `kaim56-pdftotext` container (alpine + poppler) before the built-in extractor
- Web chat paperclip takes PDF/DOCX/ODT/text via `/api/extract`; chip previews the extracted text; images keep the vision path
- App v5.30: document chip expands on tap to preview the extracted text
- App v5.29: document attachments (PDF, DOCX, ODT, TXT/MD/CSV/HTML) via admin `POST /api/extract`; `mgr/extract.py` stdlib-only, honest errors
- POST dispatcher consults the routing table first; `/api/extract` is the first table-registered POST route
- Agent strips images from history and retries when the provider rejects an image — every `myassistant` turn had died, nothing ever stored

## 2026-08-31
- Mission advance push collects completions per owner for `MISSION_COLLECT_SECS` (8 s) into one message — each push is a full `/fresh` turn
- `list_skills` returns names only (~250 tokens); `list_skills(query=…)` searches; `GET /api/skills?meta=1` serves metadata without bodies
- Type-aware offload previews: JSON outline, logs with duplicates folded and error lines kept, text head slice; `re` import added to the agent
- `mgr/routes.py` router: exact paths beat prefixes, longest prefix first; first route group migrated Strangler-style
- Route access is a route property (`@ROUTER.get(..., admin=True)`); `Router.inventory()` enumerates the surface
- Test extracts route literals from both dispatchers and fails when a prefix branch shadows an exact one
- `docs/index.html`: static landing page (isolation SVG, features, security, FAQ), no build step
- Fresh UI screenshots via DevTools protocol with instance names and spend blurred from browser-reported rectangles
- Last German UI strings translated (usage line, missions tab, mission log)

## 2026-08-30
- `tools/halo/emu_test.py`: unmodified `katagent.lua` runs on Brilliant Labs' Halo emulator; nine checks incl. the `01 00 00` packet ack
- Device Lua must be pure ASCII (runtime reads latin-1); display measured at 59 columns × 13 lines → defaults 56 × 12; app v5.28
- App v5.27 `VoiceCommand.kt`: "Photo"/"screenshot"/"stop" handled locally at utterance start; question after the command survives
- Photo by voice end to end: glasses camera → JPEG into the chat → reply on the display (`Halo.PhotoCollector`, `HaloBle.awaitPhoto()`)
- Phone screenshot recognised but not wired (needs MediaProjection or accessibility service)
- App v5.26 `HaloController.kt`: connect, upload Lua modules, click → recording → `/api/stt` → instance → reply with line wrapping
- `HaloDryLink` fakes the glasses for tests and a "Dry run" button; Settings gained a "Glasses (Halo)" section
- `HaloBle.kt`: filtered scan, MTU 517, notifications, writes paced by GATT + app-level acks; `BLUETOOTH_SCAN`/`CONNECT` permissions
- Ack check was written against the wire form while the receive side strips `0x01` — every write would have timed out; fixed
- `assets/halo/katagent.lua`: glasses-side app on the unmodified SDK modules (BSD-3-Clause); `tools/halo/test_frame_app.lua` fakes `frame`
- Stop handler no longer clears `streaming` early — it swallowed the final audio chunk `0x06`
- `Halo.kt` aligned with the SDK `plain_text` layout and message codes; ack constants and photo chunk flags added

## 2026-08-29
- `Halo.kt`: BLE protocol of the Brilliant Labs glasses ported to Kotlin (GATT, packet framing, audio chunks, WAV header); no Android deps
- Bluetooth behind the `HaloLink` interface; 15 JVM unit tests (`gradle testDebugUnitTest`); BLE implementation, Lua app and UI still to come

## 2026-08-27
- 55 skills from github.com/daedalus/skills imported (67 total); no upstream license → `skills.json` gitignored, originals in `examples/`
- Skill bodies no longer inlined in the page; editor fetches `GET /api/skills/<name>` on demand (page ~195 KB instead of ~870 KB)
- VS Code link in the footer from `CODE_URL` in gitignored `site.json` (link, not embed — mixed content)
- Missions no longer orchestrator-only: any persistent agent owns its own via `/api/missions` and `/api/mission-start|update|finish`
- `mission_update` gained `target` (executing instance), stored on the step, in the log and shown in UI/app
- Advance trigger pushes to the mission owner instead of `ORCH_INSTANCE`; admin actions resolve the owner by id (`mission_owner(mid)`)
- App v5.25: Missions screen loads all owners, shows owner and executing agent per step

## 2026-08-26
- App v5.24: non-2xx error bodies shown as one clean line (`⚠️ HTTP 503: …`) instead of raw HTML streamed into the bubble
- Manager `/i/<inst>/api/…` answers plain text 503 when the instance is not running; browser path keeps HTML

## 2026-08-24
- App reaches the manager over iroh (P2P, NAT-traversed, e2e encrypted): Rust gateway `iroh-gw` (ALPN `kaim56-mgr/0`) relays to :8700
- Auth = phone node-id allowlist re-read per connection; `mgr/irohgw.py` + `/api/iroh`, Sharing-tab card, `iroh-gw.service`, installer builds it
- App native module `app/iroh-android` (Rust + UniFFI); `IrohUrlConnection` speaks HTTP/1.1 over an iroh stream; base URL `iroh://<node-id>`

## 2026-08-22
- Full German→English pass: comments, docstrings, user-facing strings across manager, chat, agent, app, services, bridges, katfs, scripts, docs
- Prompt-block tags now English (`[Missions]`, `[Summary]`, `[Memory]`, `[Branch]`, `[Sidenote]`); personas renamed `assistant`/`translator`
- Kept as input-tolerance literals: Signal HITL `ja/nein`, `/steps unbegrenzt`, `/reasoning aus`, `/back verwerfen`, Indeed.de date words

## 2026-08-20
- New Resources tab: per-instance vCPU/RAM plus live RSS, CPU% and overlay-disk size (`/api/resources`)
- App v5.19: photo thumbnails in the bubble (Msg `image` field); on-device crash log under Settings › Diagnose
- Security gateway Layer A: scrubber also removes Unicode noncharacters and reserved default-ignorables; `text_unicode.py` now tracked
- Tool-plugin integrity: SHA-256 pinning; Plugins tab flags "changed since approve" with an Approve button; pins in `manager/plugins/.pins.json`
- Policy tab: auto-refresh no longer wipes unsaved tool-checkbox edits; Save shows real errors
- Chat sync (web + app v5.18): diverged histories adopt the newer, not-shorter server state instead of stalling forever
- App v5.17: basic Markdown in assistant messages; v5.16: streamed reply keyed by stable message key (landed in the user bubble before)
- App v5.15: read-timeout back to 600 s, mic interrupt cancels via disconnect handle — "Socket is closed" when first token took >2 s
- App v5.14/v5.13: "Assist button opens instance" setting, as a dropdown of live instances
- App v5.12: silence hang 2.2→3.5 s; tapping the mic during a reply cancels the turn and starts a fresh recording
- Tool plugins support multi-file folders; Plugins tab with drag-and-drop .py/.zip (zip-slip guarded, 5 MB cap), boilerplate, list/delete
- Web chat: Copy button on own messages; bubble text stays selectable through redraws
- New plugin `indeed_filter` (adapted from gvfullstack/JobSearchAutomation): post-filters job lists to new postings; wired into jobresearcher
- Unhandled route exceptions return HTTP 500 with a traceback instead of dropping the connection
- `playbook_add` crashed with RemoteDisconnected: `mgr/rules.py` used `uuid`/`time` without importing them
- Slash typeahead: Esc closes reliably; lists all 8 agent commands (`/fresh`, `/branch`, `/back` added), command-palette styling
- llama.cpp invalid tool-call JSON (HTTP 500): agent retries the turn once without tools
- Chat delete-tombstones sync App↔Web (app v5.11): `{id: deletedAt}` markers via `/api/chats`, 60-day TTL, `chats_tombstones.json`
- Multi-device web chat sync: awaits the server pull before selecting a conversation; open chat repaints on merge
- `_(empty reply)_` with llama.cpp/Qwen3: `reasoning_content` streams as thinking too; reasoning-only turn shows the reasoning
- Web slash typeahead fixed (undefined `escT` threw on render); `/steps` takes any count, `/steps unlimited` (0)
- Agent streams a visible tool-status (🔧) and heartbeat (`HEARTBEAT_SEC=30`) during tool execution — Traefik 180 s idle cut the stream
- Site-specific values externalised into gitignored `site.json` + `mcp-catalog.json`; source uses neutral placeholders
- Leak-filter masks HuggingFace tokens (hf_…); phone-number defaults scrubbed from templates
- Changelog and Architecture moved to a footer row — the topbar overflowed and hid them
- Guardrails at the key-injection proxy: daily token budget (`BUDGET_TOKENS`, 2M) + rate cap (`LLM_RATE_MIN`, 60/min) → 429 + notify
- Worker task-frequency cap: >6 runs/h of the same task → suspended 1 h + notify
- `EGRESS_ALLOW` per instance (domains/IPs resolved at start) via an iptables FC chain
- Leak-filter on notify/send_signal for known key patterns (sk-or-/sk-ant-/ptr_/gh*_/AKIA/xox*/JWT); git SHAs untouched
- Worker bugs from the refactoring: `chat_log_append` back in manager.py (NameError), `import re` in `mgr/store.py` (scheduled tasks looped)
- Worker hardened: each post-run step guarded, runtime orphan watch (>30 min running → requeue), diagnostic `run/worker.log`
- Notification click opens the real chat (app 5.10 + web); bell "Clear" removes notifications; slim theme scrollbars
- App 5.9: errors expire after 8 s, hints after 4 s; AGPL/SPDX header in all Kotlin sources
- `TASKS_FILE` undefined after the mgr/ split → `/api/tasks` crashed, worker died; fixed in `store.configure()` + `reclaim_stuck_tasks()`

## 2026-08-19
- Strangler-Fig refactoring done: manager.py 6,705 → ~1,400 lines core; nine `mgr/` modules (ui, store, signal, missions, notify, rules, …)
- mgr modules never import manager; cross-references via `configure()`/injection; install.sh copies `mgr/`
- Tree chat: `/branch [topic]` opens a nestable side branch, `/back` condenses it to one `[side note]` line (`/back drop` discards)
- Key-injection gateway active: agents call `/api/llm/<backend>/chat/completions`, manager injects Authorization; toggle `LLM_KEY_PROXY`
- New `oracle` tool: second opinion without tools before risky actions (`ORACLE_MODEL`); playbook forces it before `delete_task`
- Playbooks panel in the Personas tab (view/add/remove per agent)
- Installer verified end to end on a QEMU rig with nested KVM: a Firecracker microVM booted inside the test VM; smoke test accepts 401 as alive
- "Thinking" details no longer pops open on every streamed token — open/closed state preserved across repaint
- `/steps [n]` changes max tool steps at runtime; `AGENT_MAX_STEPS` permanent; jobresearcher set to 30
- New admin route `POST /api/instances/<n>/config {key,value}` sets/deletes a single config value (secrets excluded)
- `/model` switches model/backend mid-session without restart, context preserved
- Steering: a message during a running turn is injected as `[steering]` between tool steps; app 5.8; guest `POST /api/steer`
- Prompt templates as slash commands (Personas tab, `prompts.json`, `GET/POST /api/prompts`); expansion in the agent → Web, App and Signal
- Tool plugins: one .py per tool in `firecracker/plugins/` (DESC/PARAMS/REQUIRED + run()), loaded from `/config/plugins`; collisions rejected
- pi/prime templates decommissioned (never used; ~7 GB rootfs freed); source stays in git history
- `install.sh` (curl | sh): preflight, layout, Firecracker v1.16.1, kernel via `VMLINUX_URL`, rootfs/embed/mcp-hub builds, systemd unit
- Portability: `GUEST_DNS`, `CLAUDE_CRED_SRC`, `AGENT_DIR`, `FC_DIR` via env; embed/, mcp-hub/, tests/, run-tests.sh checked in
- Mission tools and `offload_read` added to `AGENT_TOOLS_CATALOG`; drift test keeps agent BUILTIN and manager catalog congruent
- Browser terminal died after ~10 s idle: connect timeout stayed as read timeout; set to None after connect plus TCP keepalive
- Overlay boot added to pi/prime/claude guest inits; `OVERLAY_ROOTFS` covers all four images
- Overlay rootfs: ro base + per-instance rw upper via overlayfs + pivot_root; no more 2 GB copy per start; degraded ro fallback
- Persistent disk per instance (default off): upper survives stop/start; `/api/instances/<n>/persist` + `/diskreset`; upper mounted `-o sync`
- Voice section in Settings: status via `GET /api/voice-health`, TTS voice and speed; service `/tts` takes `voice` + `speed`; two new voices
- Manager mixes voice/speed from settings into the `/api/tts` body — no client update needed
- Missions get their own tab (web) and screen (app 5.7); notifications carry a `link` (missions | tasks | chat:<instance>) and open the target
- Missions: `mission_start/missions/mission_update/mission_finish` (orchestrator, TASK_ADMIN), `missions.json`, survives /reset and VM restart
- Mission advance fires directly when a waited task finishes (`_mission_advance_fire`); caps 5 active / 20 steps; 7-day TTL sweep
- Missions panel in the Tasks tab and app 5.6; routes `/api/missions`, `/api/mission-*`, `/api/mission-admin`
- Chat UI (/chat) pulled onto the Industry design system (blueprint corners, Barlow Condensed, token ramps, dark mode)
- Chrome "Save password?" popup on chat switch: API-key fields were `type=password`; masked via CSS `-webkit-text-security` instead
- Model dropdown jumped back: async race in `loadModels` read the value at fetch start, now on resolve; manager page `Cache-Control: no-store`
- Model dialog knows `ORCAROUTER_MODEL` and `LLAMA_MODEL`
- Activity dialog: time filter (1h/24h/7d/All) and usage total via `GET /api/usage/<name>?since=` (`usage_for`)
- App 5.5 VAD: floor as minimum over ~0.6 s, absolute margin, hysteresis — recording cut off mid-speech after ~4 s; `VAD_HANG` 2200 ms
- Notifications: agent tool `notify(title, message)` → `POST /api/notify`; `notifications.json` with long-poll; web bell; app 5.4

## 2026-08-18
- Harness pattern (strands-agents/harness-sdk, ported): `_trim_history` summarizes the oldest messages into a `[summary]` block
- Context offloader: tool outputs > `OFFLOAD_MIN` go to `.offload/`, preview + reference stay; new tool `offload_read(id, offset)`
- `/goal <criterion>`: a judge checks the answer, up to `GOAL_MAX_ATTEMPTS`=3; app 5.2 picker
- Tool hook: hard denylist (rm -rf /, fork bomb, mkfs) always on; optional HITL approval via Signal (`HITL=1`, `HITL_TOOLS`, `/api/hitl`)
- Retry with backoff around every model call (429/5xx); empty tools list no longer sent (400 at summarizer/judge)
- E2E test suite `tests/e2e.py` / `./run-tests.sh`: OFFLINE, HTTP and LIVE tiers, stdlib unittest; runs with every change
- `/api/agents` reports all MODEL_KEYS plus a real `backend` field; agent knows its backend/model from the system prompt
- `set_model("orcarouter:tencent/hy3")` sets backend and model; orchestrator moved to OrcaRouter `tencent/hy3`
- OrcaRouter as second LLM backend: template `orcarouter.json`, `ORCAROUTER_API_KEY`/`ORCAROUTER_URL`/`ORCAROUTER_MODEL`
- Playbooks: fixed rules the orchestrator learns (`playbook_add`/`playbooks`/`playbook_forget`, `playbooks.json`, cap 40), always in the prompt
- `/reasoning [low|medium|high|off]` toggles OpenRouter reasoning; thinking streamed separately and shown as an expandable block
- `list_tasks`/`delete_task`/`edit_task` for the orchestrator only via injected `TASK_ADMIN` flag (menu and route gated)
- Signal receiving: gateway switched to `json-rpc`, stdlib WebSocket receiver; new message triggers the orchestrator immediately
- katfs file browser in the Sharing tab: share selector, "Download all" as ZIP (`/api/katfs/zip`, cap 2000 files / 512 MB); `katfs-share` CLI
- App 4.9–5.1: selectable bubble text, slash-command picker, thinking display
- MSFT task repaired with a fixed Yahoo URL and `http_fetch` instruction

## 2026-08-17
- Orchestrator token runaway (249M tokens): heartbeat `every 1m` → `every 30m`; heartbeats run stateless with `/fresh` in a throwaway context
- Sliding context cap `CTX_MAX_MSGS=20`, cut only at clean turn boundaries
- Memory instruction defused: no unprompted lectures about memory mechanics; durable facts quietly via `memory_store`

## 2026-08-16
- Semantic long-term memory: `memory_store` embeds notes (multilingual-e5 on CPU, new `embed` container :8772) into `history.db`
- Per turn the manager returns the semantically nearest notes as a `[memory]` block (`sem_store`/`sem_search`, `POST /api/memory-search`)
- App 4.8 passes unknown slash commands through to the agent (keeps `/task`, `/agents`, `/help`); `/reset` retrofitted into the claude web bridge
- Behavior guardrails from Anthropic's published system prompts in the OpenRouter/llama base prompt (no hallucination, tools first, tone)
- Self-hosted LLM via llama.cpp: settings `LLAMA_ENDPOINT` + `LLAMA_API_KEY`, template `llama`, `LLAMA_MODEL`; LAN target opened in FORWARD

## 2026-08-15
- claudy: guest route `GET /api/claude-credentials` delivers the live `claudeAiOauth` block at boot (claude template only, by source IP)
- Manager proxy unpacks JSON `{"reply": …}` from the claude bridge into text/plain for the app
- Per-instance FORWARD chain: assigned MCP endpoints + guest DNS ACCEPT, private networks REJECT, internet ACCEPT — VMs could reach the LAN
- MCP hub (`mcp-hub/`, 127.0.0.1:8771): MCP processes run once per (instance, server) on the host; guests speak JSON-RPC via `POST /api/mcp`
- Secrets injected host-side; `/api/mcp-config` delivers placeholders only; every `tools/call` audited; `mcp-portainer` removed from the image
- New Architecture tab: theme-aware SVG plus 15 component cards; `<style>` inside inline SVG and unquoted attributes were the traps
- Standing memory instruction in every system prompt; agent injects remembered facts on the first turn after restart
- ext4 journal incident: all instances of a template shared one writable rootfs; each VM now gets its own fresh copy at start
- Model chip is a button: `POST /api/instances/<n>/model` + `set_model()`, effective after stop/start
- New tool `send_signal`: sending via manager `/api/signal`; recipients must be in `ALLOWED_SENDERS`; 10 per 5 min; `SIGNAL_API` setting
- Security gateway per chat (`gateway.json`, shield icon): strips invisible Unicode both ways and EXIF/XMP/C2PA from uploads (`text_unicode.py`)
- KatAgent 4.3–4.7: speech-pause detection, tap to stop output, gateway toggle, version in drawer, digital-assistant registration
- KatAgent 4.2: microphone button (AAC/M4A 16 kHz) → `ManagerSync.stt()`, hands-free submit; speaker per reply; voice-asked replies read aloud
- Voice service (`voice/`): Parakeet TDT v3 STT, Piper TTS (Thorsten), ffmpeg; routes `/api/stt` and `/api/tts`; web microphone + Read aloud

## 2026-08-14
- Critical: POST routes were open to guests — a VM could `POST /api/instances/<n>/mounts` and NFS-mount `/` writable; guest POST allowlist
- Stored XSS fixed: `h()` (html.escape) on model id, description, mount paths and tool list in the admin page
- Open: `MANAGER_PASS` empty → `_auth()` lets everyone through; protection rests on Traefik
- Tasks and Policy tabs refresh every 15 s when visible; usage rows update from `/api/usage`
- Host timezone was `America/Chicago` → `Europe/Berlin`; manager restart needed (glibc reads the zone once)
- Web chat, radio dot and browser terminal aligned with the Industry design; katfs share page left as is
- App follows the Industry design system: angular shapes, type scale, `IndustrySpacing`, `IndustryComponents.kt`; KatAgent 3.9
- Question from the app disappeared: sync merge replaced the conversation object `send()` held; merge fills in place, append-only; KatAgent 3.8
- Token counter per instance: `llm_usage` table, guest `POST /api/usage` attributed by source IP, admin `GET /api/usage`; openrouter only
- Guests without internet: `HOSTIF=eth0` did not exist → MASQUERADE hit nothing; `HOSTIF` used only if present, else default-route interface
- `/api/chats` long-poll (`?since=<rev>&wait=<sec>`); web hangs on it; guest lock compares path without query; KatAgent 3.7 `pollChats()`
- Rebrand: "Firecracker Manager" → kAIm56 in title, wordmark, footer, realm, User-Agent; new `logo.svg` at `/logo.svg` and `/favicon.ico`
- Every template picks its LLM: `ANTHROPIC_MODEL` for claude, `PI_MODEL`/`PRIME_MODEL` as OpenRouter selects; "other model id…" free text
- UI and API messages English throughout (code comments stay German)
- Instant trigger: a new user message pokes the orchestrator debounced (8 s); 2-h heartbeat stays as fallback
- New tool `read_inbox` (`/api/inbox`): new user messages from the shared chat store with a watermark (`?peek=1`)
- New tool `list_agents` (`/api/agents`); `orchestrator` persona and instance (gemini-2.5-flash) with a 2-h heartbeat task
- SQLite `history.db` records every executed task; tool `recall_tasks(query, limit)` via `GET /api/history`
- New tool `create_task(task, target, schedule, wait)` via `POST /api/task`: `target=<instance>` or `ephemeral`; ephemeral children may not nest

## 2026-08-13
- Signal bridge turns report to `POST /api/chat-log`; appended as `sig-<instance>-<sender>` conversations, visible in App and Web
- Limit: the claude Signal bridge is not yet connected

## 2026-08-10
- Audit log per instance: every tool call reported to `/api/audit` (tool + target, never values); `audit/<name>.jsonl` on the host, admin read
- Policy tab per instance: internet toggle, model, editable tool allowlist (`POST /api/instances/<n>/tools`), secrets, MCP, share, Activity
- Instance table shows the model in use as a chip; `/chat` sends `Cache-Control: no-store`, icons inline SVG
- pi/prime fetch missing provider keys from the broker at start (`ensure_provider_keys`); build scripts get `mkfs.ext4` PATH and atomic install
- Internet toggle per instance (`internet`, live via `apply_internet`); off = no LAN, no web, no LLM
- Tool allowlist per instance (`AGENT_TOOLS`), filtered in schema and execution; `bash` remains the master key
- Tasks tab for the existing scheduler: one-off or recurring jobs to an instance; `/api/tasks` admin-only
- Color emoji swapped for inline SVGs (tofu without an emoji font)
- Diagnosis: free gemma model talks about searching but never calls `web_search`

## 2026-08-09
- katfs cross-tenant hole (critical): node bound to `0.0.0.0:8790` without caller check; now 127.0.0.1, guests via broker `/api/katfs/*`
- Guest isolation: microVMs could route to each other; tap ACCEPTs restricted, `pool->pool` DROP
- `/api/chats` and `/api/tasks` added to the guest guard; `/api/memory/<instance>` name from source IP, not path
- API keys removed from instances: agent fetches `OPENROUTER_API_KEY` via `/api/secret/<name>` against `secret-policy.json`
- `/api/settings` and `/api/instances` guest-guarded; set secrets go out as `__unchanged__`; `settings.json` 0600
- MCP tokens removed from instances: `MCP_SERVERS=<names>` plus `/api/mcp-config` with policy-released secrets only; migration lifts old blobs
- katfs multiple shares at once (stable share-id, `GET /shares`, `?share=<id>`); `KATFS_SHARE` dropdown at creation
- `katfs-share` native provider (`iroh-fs/client/`, `--ro`, auto-reconnect); `delete` operation with three locks; share page under `/katfs/`
- Manager UI rebuilt on the Industry design system: Barlow, blueprint frames, hash-routed tabs
- Folder browser (`/api/browse`, admin-only) in mount rows; Models tab (live OpenRouter catalog → `models.json`); Sharing tab
- Rootfs rebuild atomic (mv instead of cp); `KillMode=process` in the unit (not yet installed); Docker build scripts for katfs
