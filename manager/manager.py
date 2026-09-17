#!/usr/bin/env python3
# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""kAIm56 — Manager (web UI + API) for 1..x microVM instances (Firecracker).

Runs as root (needs /dev/kvm, ip, iptables) — e.g. via systemd. Pure standard
library, no extra packages. Instances are stored as JSON under
instances/<name>.json; the network is derived per instance from 'index':
  host  172.30.<index>.1/30   guest 172.30.<index>.2/30   tap fc<index>
"""
import tempfile
import base64
import codecs
import html
import json
import mimetypes
import os
import re
import shlex
import glob
import hashlib
import hmac
import pwd
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import chatui   # chat interface (/chat), lives next to this file

from mgr import paths as _paths  # noqa: E402
from mgr import instances as _instances  # noqa: E402
from mgr import secrets as _secrets  # noqa: E402
from mgr import llmproxy as _llmproxy  # noqa: E402
from mgr import ui as _ui  # noqa: E402
from mgr import routes as _routes  # noqa: E402
from mgr import katfs as _katfs  # noqa: E402
from mgr import plugins as _plugins  # noqa: E402
from mgr import voice as _voice  # noqa: E402
from mgr import browse as _browse  # noqa: E402
from mgr import audit as _audit  # noqa: E402
from mgr import auth as _auth  # noqa: E402
from mgr import host as _host  # noqa: E402
from mgr import util as _util  # noqa: E402
from mgr import models as _models  # noqa: E402
from mgr import about as _about  # noqa: E402
from mgr import settings as _settings  # noqa: E402

# Load the mgr package early: injections (notify/sem) happen further down,
# as soon as the respective functions are defined.
from mgr import missions as _missions  # noqa: E402
_missions.configure(_paths.BASE)
from mgr import mcp as _mcp  # noqa: E402
_mcp.configure(_paths.BASE)
from mgr import memfs as _memfs  # noqa: E402
_memfs.configure(_paths.BASE)
from mgr import hindsight as _hindsight  # noqa: E402
_hindsight.configure(lambda: _settings.load_settings(), log=print)   # load_settings is defined further down; called lazily
from mgr import signal as _signal_mod  # noqa: E402
_signal_mod.configure(_paths.BASE)
# Write routes that an agent VM IS ALLOWED to use. Everything else is
# administration and belongs to the admin. Without this allowlist a
# compromised VM could reach the host filesystem via /api/instances/<n>/mounts
# (the manager runs as root and exports the folder into the guest via NFS)
# or create a fresh instance for itself via /api/create — the secret allowlist,
# the tool gating and the egress rules would then be moot.
# An allowlist instead of individual checks: a new route is then closed by
# default, not open by default.
GUEST_POST_PATHS = ("/api/usage", "/api/audit", "/api/task", "/api/chat-log", "/api/trace",
                    "/api/skill-proposals", "/api/sessions-search",
                    "/api/stt", "/api/tts", "/api/signal", "/api/mcp",
                    "/api/memory-search", "/api/task-delete", "/api/task-edit",
                    "/api/playbook-add", "/api/playbook-remove", "/api/hitl",
                    "/api/notify", "/api/mission-start", "/api/mission-update",
                    "/api/mission-finish", "/api/ha-alias", "/api/ha-control")
GUEST_POST_PREFIXES = ("/api/memory/", "/api/llm/")
# Request bodies are read whole into the root process: cap them. A guest (or
# anyone on the LAN) must not be able to hand the manager a gigabyte.
BODY_MAX = 4 * 1024 * 1024            # JSON routes
BODY_MAX_LLM = 8 * 1024 * 1024        # chat completions (long contexts, images)
BODY_MAX_AUDIO = 32 * 1024 * 1024     # STT uploads


# GET paths a guest VM must never reach: the admin UI, the web chat, the katfs
# browser and the per-instance proxy /i/<name>/… (incl. the WebSocket
# terminal). Only POST was gated so far — a VM could open the SHELL of every
# other running VM through GET /i/<other>/term.
GUEST_GET_DENIED_EXACT = ("/", "/chat", "/katfs")
GUEST_GET_DENIED_PREFIXES = ("/i/", "/katfs/")


def guest_get_blocked(path):
    """True when a guest VM may not GET this path (query string ignored)."""
    p = path.split("?", 1)[0]
    if p != "/" and p.endswith("/") and p[:-1] in GUEST_GET_DENIED_EXACT:
        p = p[:-1]
    return p in GUEST_GET_DENIED_EXACT or p.startswith(GUEST_GET_DENIED_PREFIXES)
POOL = "172.30.0.0/16"

_mcp.HUB_TZ = _host.HOST_TZ          # hub processes (caldav-mcp …) format dates in this zone
# ---- NFS / host folders ----------------------------------------------------
# Every export is per instance and per guest IP: the workspace
# AGENT_ROOT/<instance> and the host folders bind-mounted under
# AGENT_ROOT/.fcmnt/<instance>/<idx>. There is NO pool-wide root export any
# more (it let every VM read and write every other VM's files, and crossmnt
# handed a client its neighbours' submounts); NFSv4 serves the exports from
# its pseudo-root, the guest mounts them by absolute path. Inside the VM the
# agent is uid 1000; on the host every access is squashed to GUEST_USER, a
# system user that owns nothing but these folders.
AGENT_ROOT = os.environ.get("AGENT_ROOT", os.path.join(_paths.HOME_DIR, "agent"))
AGENT_EXPORTS = "/etc/exports.d/agent.exports"       # the retired root export
EXPORTS_D = "/etc/exports.d"
FCMNT_ROOT = os.path.join(AGENT_ROOT, ".fcmnt")
GUEST_USER = os.environ.get("GUEST_USER", "kaim56-guest")
GUEST_UID = GUEST_GID = 1000          # until ensure_guest_user() resolved the user
def ensure_guest_user():
    """Resolve (root: create) the squash user. False when it does not exist
    and cannot be created — exports then fall back to uid 1000, as before."""
    global GUEST_UID, GUEST_GID
    try:
        pw_ = pwd.getpwnam(GUEST_USER)
    except KeyError:
        if os.geteuid() != 0:
            return False
        _util.sh("useradd", "-r", "-M", "-d", "/nonexistent", "-s", "/usr/sbin/nologin", GUEST_USER, check=False)
        try:
            pw_ = pwd.getpwnam(GUEST_USER)
        except KeyError:
            return False
    GUEST_UID, GUEST_GID = pw_.pw_uid, pw_.pw_gid
    return True


def own_guest_dir(path, mode=0o2750):
    """A folder the guests write: owned by the squash user, group = operator
    (setgid, so the operator can read what the agent produces), nobody else."""
    try:
        os.makedirs(path, exist_ok=True)
        if os.geteuid() == 0:
            os.chown(path, GUEST_UID, _host.ADMIN_GID)
        os.chmod(path, mode)
    except OSError as e:
        print(f"[quiet] own_guest_dir {path}: {e!r}", flush=True)


def workspace_dir(inst):
    return os.path.join(AGENT_ROOT, inst["name"])


def export_opts(ro, fsid):
    return (f"{'ro' if ro else 'rw'},sync,no_subtree_check,all_squash,"
            f"anonuid={GUEST_UID},anongid={GUEST_GID},fsid={fsid}")

os.makedirs(_paths.RUN_DIR, exist_ok=True)


# ---- Signal (send/HITL/receive): moved out to mgr/signal.py ---------------


# ---- Security gateway: moved out to mgr/gateway.py -------------------------
from mgr import gateway as _gateway  # noqa: E402
_gateway.configure(_paths.BASE)


# ---- Chat history (sync with the app) --------------------------------------
CHATS_FILE = os.path.join(_paths.BASE, "chats.json")
TOMBSTONES_FILE = os.path.join(_paths.BASE, "chats_tombstones.json")
TOMB_TTL_MS = 60 * 24 * 3600 * 1000   # discard deletion markers after 60 days


def load_tombstones():
    try:
        with open(TOMBSTONES_FILE) as fh:
            d = json.load(fh)
        return {str(k): int(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError, TypeError):
        return {}


def save_tombstones(t):
    now = int(time.time() * 1000)
    t = {k: v for k, v in t.items() if now - v < TOMB_TTL_MS}   # TTL prune
    try:
        with open(TOMBSTONES_FILE, "w") as fh:
            json.dump(t, fh)
    except OSError as e:
        # a lost tombstone resurrects deleted chats on the next sync
        print(f"[quiet] tombstones save failed: {e!r}", flush=True)
    return t


# One lock for every load->modify->save cycle on chats.json: the task worker
# appends results while the app's sync POST merges its state — unguarded, the
# later save silently drops the other side's messages.
_chats_rmw_lock = threading.Lock()


def load_chats():
    try:
        with open(CHATS_FILE) as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return []


def save_chats(data):
    if not isinstance(data, list):
        return -1
    try:
        with open(CHATS_FILE, "w") as fh:
            json.dump(data, fh)
        bump_chats_rev()          # immediately wake waiting long-polls (app/web)
        return len(data)
    except OSError:
        return -1


# Live sync: every write to the chat store bumps a revision. App and web hang
# on the long-poll with ?since=<rev>&wait=<sec> and see the other side's message
# within fractions of a second — no reload, no constant polling. Without the
# parameters, /api/chats responds as before (a list), so older clients keep
# working unchanged.
_chats_cv = threading.Condition()
try:
    _chats_rev = int(os.path.getmtime(CHATS_FILE) * 1000)
except OSError:
    _chats_rev = 0


def bump_chats_rev():
    global _chats_rev
    with _chats_cv:
        # Time-based, but strictly monotonic: survives a manager restart without
        # leaving a client with an old `since` stuck.
        _chats_rev = max(_chats_rev + 1, int(time.time() * 1000))
        _chats_cv.notify_all()


def wait_chats(since, timeout):
    """(rev, chats|None) — the list only if something changed since `since`,
    otherwise None (timeout). Blocks at most `timeout` seconds."""
    deadline = time.time() + max(0.0, timeout)
    with _chats_cv:
        while _chats_rev <= since:
            rest = deadline - time.time()
            if rest <= 0:
                break
            _chats_cv.wait(min(1.0, rest))
        rev = _chats_rev
    return rev, (load_chats() if rev > since else None)


# ---- Notifications: moved out to mgr/notify.py -----------------------------
from mgr import notify as _notify  # noqa: E402
_notify.configure(_paths.BASE)
_missions.notify_add = _notify.notify_add   # injection (mgr/missions)


# ---- Inbox (watermark) — coupled to chat, stays here -----------------------
INBOX_WM_FILE = os.path.join(_paths.BASE, "inbox_wm.json")


def _inbox_wm():
    try:
        with open(INBOX_WM_FILE) as fh:
            return int(json.load(fh).get("ts", 0))
    except (OSError, ValueError):
        return 0


def inbox_since(peek=False):
    """New user messages from the shared chat store (Signal/app/web) since the
    last run — as an inbox for the orchestrator. Watermark over
    conversation.updatedAt: every conversation with new activity is delivered
    once (last user message). Task conversations (results) are hidden.
    peek=True delivers without setting the watermark."""
    wm = _inbox_wm()
    items, maxts = [], wm
    for c in load_chats():
        if not isinstance(c, dict) or str(c.get("id", "")).startswith("task-"):
            continue
        ut = int(c.get("updatedAt", 0) or 0)
        if ut <= wm:
            continue
        maxts = max(maxts, ut)
        last_user = next((m.get("text", "") for m in reversed(c.get("messages", []) or [])
                          if m.get("user")), "")
        if last_user:
            item = {"instance": c.get("instance", ""), "title": c.get("title", ""),
                    "id": c.get("id", ""), "text": last_user}
            if str(c.get("id", "")).startswith("sig-"):
                # Filed by the instance's own agent (/api/chat-log), not typed
                # into the app: the orchestrator sees WHO relayed it.
                item["via"] = f"signal:{c.get('instance', '')}"
                item["text"] = f"[Signal message relayed by agent '{c.get('instance', '')}'] {last_user}"
            items.append(item)
    if not peek and maxts > wm:
        try:
            with open(INBOX_WM_FILE, "w") as fh:
                json.dump({"ts": maxts}, fh)
        except OSError as e:
            # a stale watermark re-delivers old inbox messages to the orchestrator
            print(f"[quiet] inbox watermark save failed: {e!r}", flush=True)
    return items


def chat_log_append(inst_name, sender, user_text, reply_text, kind="signal"):
    """Append a turn (question + answer) to the shared chat history so it shows
    up in the app and web. `kind`='signal' -> one conversation per
    (instance, sender); 'task' -> one task conversation per instance;
    'voice' -> `sender` IS the conversation id (a voice session, see
    voice_session), titled with its start time so archived sessions are
    telling apart in the list."""
    if kind == "task":
        cid = f"task-{inst_name}"
        title = f"Tasks · {inst_name}"
    elif kind == "voice":
        cid = str(sender)
        title = f"Voice · {inst_name} · " + time.strftime("%d.%m. %H:%M")
    else:
        sid = re.sub(r"[^a-zA-Z0-9]", "", (sender or "signal"))[:20] or "signal"
        cid = f"sig-{inst_name}-{sid}"
        title = f"Signal · {inst_name}"
    with _chats_rmw_lock:
        chats = load_chats()
        conv = next((c for c in chats if isinstance(c, dict) and c.get("id") == cid), None)
        now = int(time.time() * 1000)
        if conv is None:
            conv = {"id": cid, "title": title, "mode": "server",
                    "instance": inst_name, "messages": [], "updatedAt": now}
            chats.append(conv)
        if user_text:
            conv["messages"].append({"user": True, "text": str(user_text)})
        if reply_text:
            conv["messages"].append({"user": False, "text": str(reply_text)})
        conv["messages"] = conv["messages"][-500:]
        conv["updatedAt"] = now
        n = save_chats(chats)
    try:
        _memfs.timeline_add(inst_name, kind, user_text, reply_text)   # the agent's own timeline
        # A-1: keep the USER turn only. The agent's own reply must NOT become a
        # remembered "fact" — a wrong answer would otherwise feed back into
        # recall as truth (memory poisoning). Explicit memory_store notes still
        # go in (below); this is the passive chat capture.
        if user_text and _instances.hindsight_retains(inst_name):
            _hindsight.retain_async(inst_name, str(user_text), (kind or "chat", "user"))
    except Exception as e:
        print(f"[quiet] memfs timeline failed: {e!r}", flush=True)
    return n


# ---- Voice sessions: what a voice client says shows up in the web chat ------
# /api/chat/<inst> is what the desktop client and self-built devices (ESP32)
# call. Until now those turns lived only in the VM's own history — invisible
# on agents.kat56.de. Now every turn is appended to the shared store under a
# session conversation; "/reset" rotates the session, the old conversation
# stays as the archive. Web chat turns (numeric ids) are NOT mirrored: the
# web page stores them itself.
_voice_sessions = {}          # (instance, source ip) -> session id
_voice_lock = threading.Lock()


def voice_session(inst_name, src, client_id="", reset=False):
    """Conversation id for a caller of /api/chat/<inst>, or '' when the turn
    is not a voice turn. A client that sends a 'voice-…' chat id (desktop
    client) owns the rotation; one without a chat id (ESP) gets a manager-kept
    session that '/reset' rotates. Any other id belongs to the web chat."""
    cid = str(client_id or "")
    if cid:
        if not cid.startswith("voice-"):
            return ""
        return f"voice-{inst_name}-" + re.sub(r"[^a-zA-Z0-9_-]", "", cid[len("voice-"):])[:40]
    key = (inst_name, src)
    with _voice_lock:
        if reset:
            _voice_sessions.pop(key, None)
            return ""
        sid = _voice_sessions.get(key)
        if not sid:
            sid = f"voice-{inst_name}-{time.strftime('%Y%m%d-%H%M%S')}"
            _voice_sessions[key] = sid
        return sid


def merge_chats(incoming):
    """MERGE chats (newer updatedAt wins) plus DELETION TOMBSTONES:
    `incoming` is either a bare list (old: chats only) or an object
    {chats:[...], tombstones:{id:deletedAt}}. A tombstoned chat does not come
    back — not even through a re-push from the app — as long as its updatedAt is
    not NEWER than the deletion (a genuine edit after the deletion resurrects it
    and discards the tombstone). Tombstones have a TTL."""
    if isinstance(incoming, dict):
        chats_in = incoming.get("chats") or []
        tombs_in = incoming.get("tombstones") or {}
    else:
        chats_in = incoming if isinstance(incoming, list) else []
        tombs_in = {}

    tombs = load_tombstones()
    if isinstance(tombs_in, dict):
        for k, v in tombs_in.items():
            try:
                tombs[str(k)] = max(tombs.get(str(k), 0), int(v))
            except (TypeError, ValueError):
                continue

    with _chats_rmw_lock:
        by_id = {}
        for c in load_chats():
            if isinstance(c, dict) and c.get("id") and c.get("messages"):
                by_id[str(c["id"])] = c
        for c in chats_in if isinstance(chats_in, list) else []:
            if not isinstance(c, dict) or not c.get("id") or not c.get("messages"):
                continue
            cid = str(c["id"])
            cur = by_id.get(cid)
            if cur is None or c.get("updatedAt", 0) >= cur.get("updatedAt", 0):
                by_id[cid] = c

        # Apply tombstones. A chat only resurrects when it was GENUINELY edited
        # after the deletion. A bogus far-future updatedAt (seen: a leaked sync
        # test fixture dated year 2286, updatedAt 1e13) must NOT beat a real
        # deletion, or the chat becomes undeletable. A real ms timestamp will
        # not reach the year-2100 ceiling for ~75 years, so anything past it is
        # garbage and cannot resurrect.
        TS_CEIL = 4102444800000             # 2100-01-01 in ms
        for cid, dat in list(tombs.items()):
            c = by_id.get(cid)
            eff = c.get("updatedAt", 0) if c is not None else 0
            if eff > TS_CEIL:
                eff = 0                     # implausible timestamp -> not a real edit
            if c is not None and eff > dat:
                tombs.pop(cid, None)        # chat genuinely newer -> resurrection ok
            else:
                by_id.pop(cid, None)        # deleted stays deleted

        save_tombstones(tombs)
        merged = sorted(by_id.values(), key=lambda x: x.get("updatedAt", 0), reverse=True)
        return save_chats(merged)





# ---- Background jobs (task queue + scheduler) ------------------------------

# ---- store: SQLite history/usage/semantics + memory -> mgr/store.py -------
from mgr import store as _store  # noqa: E402
_store.configure(_paths.BASE)
_missions.sem_store = _store.sem_store   # injection (mgr/missions)


TASK_TIMEOUT = int(os.environ.get("TASK_TIMEOUT", "1800"))    # worker-run tasks: 30 min


def _chat_post(inst, message, timeout=600):
    """Non-streaming chat call to an instance's bridge. The agent gets the
    deadline along and stops its tool loop in time — a run that outlives the
    caller answers into the void (a 12-step job search once did)."""
    url = f"http://{_instances.net_of(inst)['guest']}:{_instances.WEB_GUEST_PORT}/api/chat"
    data = json.dumps({"message": message, "deadline": time.time() + timeout - 30,
                       "kind": "task"}).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    body = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
    try:
        return json.loads(body).get("reply", body)
    except ValueError:
        return body


def _run_named(instance, message, timeout=600):
    """Run a task on an EXISTING instance (its tools/MCP/secrets live there).
    Starts it if needed and waits until the bridge is up."""
    inst = next((i for i in _instances.load_instances() if i["name"] == instance), None)
    if not inst:
        return (False, f"instance '{instance}' unknown")
    if not _instances.is_running(inst):
        if not wait_web(inst, timeout=120):
            return (False, f"instance '{instance}' not ready")
        inst = next((i for i in _instances.load_instances() if i["name"] == instance), None)
    try:
        return (True, _chat_post(inst, message, timeout=timeout))
    except (TimeoutError, socket.timeout):
        return (False, f"error: no answer within {timeout} s — the agent did not finish in time")
    except Exception as e:
        return (False, f"error: {e!r}")


EPHEMERAL_MAX = int(os.environ.get("EPHEMERAL_MAX", "2"))
_ephemeral_slots = threading.BoundedSemaphore(EPHEMERAL_MAX)


# A sandboxed run: an ephemeral VM with a NARROWER policy than its caller —
# fewer tools, an egress allowlist or no network at all, and optionally one
# skill baked into its system prompt. The cage is the Firecracker VM as
# always; what changes is what the agent inside may do. A caller can only
# narrow: no tool it does not hold itself, no host outside its own allowlist.
SANDBOX_DEFAULT_TOOLS = ["bash", "read_file", "write_file", "list_dir", "offload_read",
                         "http_fetch", "web_search", "read_pdf"]
SANDBOX_NEVER = {"spawn_subagent", "create_task", "send_signal", "notify", "get_secret", "list_secrets"}


def sandbox_config(caller, sandbox):
    """(cfg, internet, error) for an ephemeral VM from a sandbox request
    {"tools": …, "egress": …, "skill": name, "persona": name}.
    Empty request = the ephemeral VM as before (all tools, internet on)."""
    sb = sandbox or {}
    if not isinstance(sb, dict):
        return {}, True, "sandbox must be an object"
    ccfg = (caller or {}).get("config") or {}
    cat = ccfg.get("AGENT_TOOLS", "")
    caller_tools = {t.strip() for t in cat.split(",") if t.strip()} if cat else None   # None = all
    want = sb.get("tools") or []
    if isinstance(want, str):
        want = want.split(",")
    want = [str(t).strip() for t in want if str(t).strip()]
    skill = str(sb.get("skill") or "").strip()
    persona = str(sb.get("persona") or "").strip()
    pobj = next((p for p in load_personas() if p.get("name") == persona), None) if persona else None
    if persona and pobj is None:
        return {}, True, f"persona '{persona}' unknown"
    if not want and pobj and pobj.get("tools"):
        want = list(pobj["tools"])          # the persona's recommended tool subset
    if not want and skill:
        want = list(SANDBOX_DEFAULT_TOOLS)
    cfg = {}
    if want:
        unknown = sorted(set(want) - AGENT_TOOL_NAMES)
        if unknown:
            return {}, True, f"unknown tools: {', '.join(unknown)}"
        if caller_tools is not None:
            over = sorted(set(want) - caller_tools)
            if over:
                return {}, True, f"the caller does not hold these tools itself: {', '.join(over)}"
        cfg["AGENT_TOOLS"] = ",".join(sorted(set(want) - SANDBOX_NEVER))
    internet = True
    eg = sb.get("egress")
    if eg is not None and eg != "":
        if eg is False or (isinstance(eg, str) and eg.strip().lower() in ("none", "off", "no")):
            internet = False
        else:
            hosts = eg.split(",") if isinstance(eg, str) else list(eg)
            hosts = [str(x).strip().lower() for x in hosts if str(x).strip()]
            if not hosts:
                return {}, True, "egress: list hosts, or 'none'"
            ceg = [x.strip().lower() for x in (ccfg.get("EGRESS_ALLOW") or "").split(",") if x.strip()]
            if ceg:
                over = sorted(set(hosts) - set(ceg))
                if over:
                    return {}, True, f"egress outside the caller's own allowlist: {', '.join(over)}"
            cfg["EGRESS_ALLOW"] = ",".join(hosts)
    base = (pobj.get("prompt") if pobj else None) \
        or next((p.get("prompt", "") for p in load_personas() if p.get("name") == "assistant"), "") \
        or "You are a helpful agent with tools. Use them when needed, otherwise answer directly. Be concise."
    if pobj and pobj.get("model") and "OPENROUTER_MODEL" not in cfg:
        cfg["OPENROUTER_MODEL"] = str(pobj["model"])[:120]
    if skill:
        body = next((s.get("content", "") for s in load_skills() if s.get("name") == skill), None)
        if body is None:
            return {}, True, f"skill '{skill}' unknown"
        cfg["AGENT_SYSTEM"] = f"{base}\n\n[Skill: {skill}] Follow this skill for the task:\n{str(body)[:20000]}"
    elif pobj:
        cfg["AGENT_SYSTEM"] = base
    return cfg, internet, ""


def _run_ephemeral(message, model=None, timeout=600, sandbox=None):
    """Run a task in a FRESH, isolated VM that is deleted afterwards — at most
    EPHEMERAL_MAX at a time: every one is a full VM (RAM, tap, disk), and any
    guest may ask for one, so the rest queue instead of exhausting the host."""
    if not _ephemeral_slots.acquire(timeout=600):
        return (False, f"ephemeral VM slots busy ({EPHEMERAL_MAX} at a time) — try again later")
    try:
        return _run_ephemeral_vm(message, model, timeout, sandbox)
    finally:
        _ephemeral_slots.release()


def _run_ephemeral_vm(message, model=None, timeout=600, sandbox=None):
    name = "task-" + uuid.uuid4().hex[:6]
    cfg = {"TRANSPORT": "web", "NO_SPAWN": "1"}
    internet = True
    if sandbox:                                   # {"cfg": {...}, "internet": bool} from sandbox_config
        cfg.update(sandbox.get("cfg") or {})
        internet = bool(sandbox.get("internet", True))
    if model:
        cfg["OPENROUTER_MODEL"] = model           # an explicit model wins over a persona's model
        print(f"[ephemeral] {name}: sandbox tools={cfg.get('AGENT_TOOLS') or 'all'} "
              f"egress={cfg.get('EGRESS_ALLOW') or ('none' if not internet else 'any')}"
              f"{' skill' if 'AGENT_SYSTEM' in cfg else ''}", flush=True)
    msg = create_instance(name, "openrouter", cfg, internet=internet)
    inst = next((i for i in _instances.load_instances() if i["name"] == name), None)
    if not inst:
        return (False, f"ephemeral VM failed: {msg}")
    try:
        if not wait_web(inst, timeout=120):
            return (False, "ephemeral VM not ready")
        return (True, _chat_post(inst, message, timeout=timeout))
    except Exception as e:
        return (False, f"error: {e!r}")
    finally:
        try:
            stop(inst)
            delete_instance(name)
        except Exception as e:
            # a leaked ephemeral VM keeps its tap, its disk and its RAM
            print(f"[quiet] ephemeral cleanup of {name} failed: {e!r}", flush=True)


def resolve_task_target(target):
    """('name', '') or ('', error). Strips a leading '@' — an agent once wrote
    '@orchestrator' and the task then failed every morning for days with
    'instance unknown' while nobody was told — and refuses unknown names at
    creation time instead of at 08:00 the next day."""
    t = (target or "").strip().lstrip("@").strip() or "ephemeral"
    if t == "ephemeral" or any(i.get("name") == t for i in _instances.load_instances()):
        return t, ""
    return "", f"instance '{t}' unknown (targets: ephemeral or an existing instance name)"


def unknown_target_tasks(tasks, names):
    """Scheduled/pending tasks whose instance does not exist (and is not
    'ephemeral') and that were not reported yet — the ones that would fail at
    their next run without anyone hearing about it."""
    out = []
    for t in tasks:
        inst = str(t.get("instance") or "")
        if t.get("status") in ("scheduled", "pending") and inst != "ephemeral" \
                and inst not in names and not t.get("target_warned"):
            out.append(t)
    return out


def task_target_sweep():
    """Hourly (idle worker): push once per task with a dead target, then mark
    it so the push does not repeat. Editing the task clears the mark."""
    names = {i.get("name") for i in _instances.load_instances()}
    hit = []

    def mut(tasks):
        for t in unknown_target_tasks(tasks, names):
            t["target_warned"] = int(time.time())
            hit.append((t.get("id"), t.get("instance"), str(t.get("message", ""))[:120]))
        return bool(hit), None
    _store.with_tasks(mut)
    for tid, inst, msg in hit:
        try:
            _notify.notify_add("task", f"Task target unknown: {tid}",
                       f"instance '{inst}' does not exist — {msg}", link="tasks")
        except Exception as e:
            _util._wlog(f"{tid}: target-sweep notify: {e!r}")
    return hit


def _run_task_now(instance, message, model=None, timeout=600, sandbox=None):
    """Run a task — on a named instance (routing to the capability) or in an
    ephemeral VM (target == 'ephemeral'). `model` applies to the ephemeral VM
    only — a named instance keeps its own configuration. `timeout` is the
    caller's patience; the worker allows TASK_TIMEOUT, a waiting guest 600 s."""
    if instance == "ephemeral":
        if sandbox:
            return _run_ephemeral(message, (model or "").strip()[:120] or None, timeout, sandbox)
        return _run_ephemeral(message, (model or "").strip()[:120] or None, timeout)
    return _run_named(instance, message, timeout)


# ---- Instant trigger for the orchestrator ----------------------------------
# New user message (Signal/app/web) -> the orchestrator runs debounced within
# seconds instead of only at the next 2-h heartbeat. Coalesces bursts, one run
# at a time; if new messages arrived during the run, it fires again right away.
# Fires only if the inbox really has something new (peek).
ORCH_INSTANCE = "orchestrator"


def delegate_targets(inst):
    """Instances this guest may address besides itself and 'ephemeral': the
    DELEGATE_TARGETS list of its config (comma-separated, '*' = all). The
    orchestrator may address everything — routing work is its job."""
    if inst.get("name") == ORCH_INSTANCE:
        return {"*"}
    raw = (inst.get("config") or {}).get("DELEGATE_TARGETS", "") or ""
    return {x.strip() for x in str(raw).split(",") if x.strip()}


def guest_may_target(inst, target):
    """May this guest create a task for (and see) `target`? Own name and
    'ephemeral' always, anything else only via DELEGATE_TARGETS. Closes the
    path where a prompt-injected agent runs its text on ANY other instance —
    with that instance's secrets and MCPs."""
    target = (target or "ephemeral").strip()
    if target in ("ephemeral", inst.get("name")):
        return True
    allow = delegate_targets(inst)
    return "*" in allow or target in allow
ORCH_HEARTBEAT_MSG = (
    "/fresh "   # stateless: own throwaway context, no bloat, no wiping out a
                # running app chat (shared _history).
    "Heartbeat (instant trigger): 1) read_inbox — new user messages. "
    "2) For each one that needs action: recall_tasks (no duplicates), then "
    "list_agents and create_task to the CAPABLE instance (e.g. hass for "
    "HomeAssistant) or ephemeral. 3) Messages starting with [Signal] came in "
    "via Signal: send the reply or confirmation back with send_signal "
    "(briefly) — WITHOUT specifying a number/recipient, it goes to the user "
    "automatically; do NOT invent a number. 4) Check missions: is a step stuck "
    "on doing even though its task finished long ago (recall_tasks)? Then "
    "mission_update and kick off the next step. Keep it short. Nothing to do? "
    "Report: nothing to do.")
MISSION_ADVANCE_MSG = (
    "/fresh Mission progress (instant trigger after task completion): the "
    "following tasks are done:\n{done}\n"
    "For EACH of them: 1) recall_tasks for the result. 2) mission_update: set "
    "the step to done/failed, record the result briefly. 3) Kick off the NEXT "
    "open step (create_task to the capable instance or ephemeral, note the "
    "task-id on the step via mission_update). 4) No open step left? "
    "mission_finish with a short summary. Blocked? notify the user. Keep it short.")


# Collect mode for the advance push (idea from OpenClaw's queue modes): every
# push is a full /fresh turn and costs its fixed ~5k input tokens before any
# work happens. When several tasks finish close together — exactly what the
# cross-instance missions produce — one push handling all of them does the same
# work for one fixed cost. Completions are therefore collected per OWNER for a
# short window and flushed as a single message.
MISSION_COLLECT_SECS = float(os.environ.get("MISSION_COLLECT_SECS", "8"))
_madv_lock = threading.Lock()
_madv_pending = {}        # owner -> [ "task 'id' (mission 'mid', goal, step N)" ]
_madv_timer = {}          # owner -> threading.Timer


def _mission_advance_flush(inst):
    with _madv_lock:
        _madv_timer.pop(inst, None)
        lines = _madv_pending.pop(inst, [])
    if not lines:
        return
    if not any(i.get("name") == inst for i in _instances.load_instances()):
        return          # owner deleted -> nothing to push to (TTL sweep pauses it)
    msg = MISSION_ADVANCE_MSG.format(done="\n".join("- " + x for x in lines))
    try:
        _run_named(inst, msg)
    except Exception as e:
        print("mission-advance:", repr(e), flush=True)


def _mission_advance_fire(task_id):
    """After task completion: if the task belongs to a mission step, note it for
    the mission's OWNER and (re)arm that owner's collect window. The owner is
    whichever agent planned the mission — the step itself may have run on a
    completely different instance."""
    inst, m, st = _missions.mission_for_task(task_id)
    if not m or not inst:
        return
    line = f"task '{task_id}' (mission '{m['id']}', {m['goal'][:80]}, step {st['n']})"
    with _madv_lock:
        _madv_pending.setdefault(inst, []).append(line)
        t = _madv_timer.get(inst)
        if t:
            t.cancel()
        t = threading.Timer(MISSION_COLLECT_SECS, _mission_advance_flush, args=(inst,))
        t.daemon = True
        _madv_timer[inst] = t
        t.start()


_orch_lock = threading.Lock()
_orch_timer = [None]
_orch_running = [False]
_orch_dirty = [False]


def orchestrator_ping():
    if not any(i.get("name") == ORCH_INSTANCE for i in _instances.load_instances()):
        return
    try:
        if not inbox_since(peek=True):   # only fire if there is really something new
            return
    except Exception:
        return
    with _orch_lock:
        if _orch_timer[0]:
            _orch_timer[0].cancel()
        t = threading.Timer(8.0, _orch_fire)
        t.daemon = True
        _orch_timer[0] = t
        t.start()


# Supply the signal module with its cross-references (all now defined).
_signal_mod.load_settings = _settings.load_settings
_signal_mod.chat_log_append = chat_log_append
_signal_mod.orchestrator_ping = orchestrator_ping

def _orch_fire():
    with _orch_lock:
        if _orch_running[0]:
            _orch_dirty[0] = True
            return
        _orch_running[0] = True
    try:
        _run_named(ORCH_INSTANCE, ORCH_HEARTBEAT_MSG)
    except Exception as e:
        print("orch-trigger:", repr(e), flush=True)
    finally:
        with _orch_lock:
            _orch_running[0] = False
            rerun = _orch_dirty[0]
            _orch_dirty[0] = False
        if rerun:
            orchestrator_ping()


_mi_sweep_ts = [0.0]


def reclaim_stuck_tasks():
    """Reset orphaned 'running' tasks at startup. Exactly ONE worker runs — what
    is still 'running' at startup belongs to a crashed run (e.g. the store bug
    on Aug 20) and would otherwise never fire again."""
    def mut(tasks):
        n = 0
        for t in tasks:
            if t.get("status") == "running":
                t["status"] = "scheduled" if t.get("schedule") else "pending"
                n += 1
        return bool(n), n
    n = _store.with_tasks(mut)
    if n:
        print(f"[worker] {n} orphaned 'running' task(s) reset", flush=True)


def worker_claim(tasks, now, hb_idle, skipped_hb, throttled):
    """One claim pass over the task store (runs INSIDE with_tasks): skip idle
    heartbeats, throttle loops, claim the first runnable task. Module-level so
    the dirty contract is testable — the bug this guards against: a skip
    mutates next_run, and returning dirty=False threw that mutation away, so
    the same heartbeat was re-skipped every 5 s (9,961 log lines)."""
    def due(t):
        if t.get("status") == "running":
            return False
        if t.get("schedule"):
            return t.get("next_run", 0) <= now
        return t.get("status") == "pending"

    for t in tasks:
        if not due(t):
            continue
        if hb_idle(t):
            t["next_run"] = _store._next_run(t["schedule"], now)
            t["result"] = "skipped: inbox empty, no active mission"
            t["updated"] = now
            skipped_hb.append(t["id"])
            continue
        # Frequency cap: more than 6 runs/h of the same task is ALWAYS a
        # defect (loop bug Aug 20) — pause it for an hour.
        runs = [x for x in t.get("recent_runs", []) if now - x < 3600]
        if len(runs) >= 6:
            t["recent_runs"] = runs
            t["next_run"] = now + 3600
            throttled.append((t["id"], str(t.get("message", ""))[:120]))
            continue
        t["recent_runs"] = runs + [now]
        t["status"] = "running"
        t["updated"] = now
        return True, dict(t)
    # Skips und Drosselungen VERAENDERN Tasks (next_run!) — ohne dirty=True
    # verfiele das Weiterplanen beim naechsten Zyklus.
    return bool(throttled) or bool(skipped_hb), None


def _task_worker():
    """Processes due/pending tasks sequentially in the background."""
    reclaim_stuck_tasks()
    while True:
        ran = False
        try:
            now = int(time.time())

            # The CLAIM happens on a fresh load inside the store lock
            # (worker_claim above) — this closes the lost-update window
            # between worker and HTTP threads.
            throttled = []

            def heartbeat_idle(t):
                """The 30-min heartbeat is a full /fresh turn whose usual
                outcome is "nothing to do": ~1.560 of the orchestrator's 1.774
                audit entries in 14 days were its idle ritual. When the inbox
                holds nothing new AND no mission is active, the manager can
                answer that question itself — without waking the model. Costs
                are no argument anymore (hy3 is ~free), but the audit noise
                drowns the saddler and the free tier is a cluster risk."""
                # Only the SCHEDULED heartbeat: a one-off with the same text
                # (mission advance, manual poke) must run — and skipping a
                # pending one-off would just re-skip it every worker cycle.
                if not t.get("schedule") \
                        or not str(t.get("message", "")).startswith("/fresh Heartbeat"):
                    return False
                try:
                    if inbox_since(peek=True):
                        return False
                    if any(m.get("status") == "active"
                           for lst in _missions.load_missions().values() for m in lst):
                        return False
                except Exception:
                    return False          # in doubt: run it
                return True

            skipped_hb = []
            t = _store.with_tasks(lambda ts: worker_claim(ts, now, heartbeat_idle,
                                                   skipped_hb, throttled))
            for tid in skipped_hb:
                _util._wlog(f"{tid}: heartbeat skipped (idle — no inbox, no mission)")
            for tid, tmsg in throttled:
                _util._wlog(f"{tid}: >6 runs/h — paused for 1 h (loop protection)")
                try:
                    _notify.notify_add("guardrail", f"Task loop throttled: {tid}",
                               tmsg + " — ran >6x/h, paused 1 h.", link="tasks")
                except Exception:
                    pass
            if t is not None:
                sched = bool(t.get("schedule"))
                # From here on EVERYTHING is guarded individually: an error
                # anywhere must never leave the task as a "running" orphan
                # (bug Aug 20: exception in the follow-up -> outer except ->
                # the task never fired again and the chat entry was missing).
                try:
                    ok, res = _run_task_now(t["instance"], t["message"], t.get("model"), timeout=TASK_TIMEOUT,
                                            sandbox=t.get("sandbox"))
                except Exception as e:
                    ok, res = False, f"worker-exception (run): {e!r}"
                    _util._wlog(f"{t['id']}: {res}")
                def done_mut(fresh, _tid=t["id"], _ok=ok, _res=res, _sched=sched):
                    tt = next((x for x in fresh if x["id"] == _tid), None)
                    if tt is None:
                        return False, None
                    tt["updated"] = int(time.time())
                    tt["result"] = _res
                    if _sched:
                        tt["status"] = "scheduled"
                        tt["next_run"] = _store._next_run(tt["schedule"], int(time.time()))
                    else:
                        tt["status"] = "done" if _ok else "error"
                    return True, None
                try:
                    _store.with_tasks(done_mut)
                except Exception as e:
                    _util._wlog(f"{t['id']}: status update failed: {e!r}")
                try:
                    chat_log_append(t.get("instance", "task"), "task",
                                    t.get("message", ""), res, kind="task")
                except Exception as e:
                    _util._wlog(f"{t['id']}: chat_log_append: {e!r}")
                try:
                    _store.history_add(t.get("instance", ""), t.get("message", ""), res, ok,
                                t.get("schedule", ""), origin="worker")
                except Exception as e:
                    _util._wlog(f"{t['id']}: history_add: {e!r}")
                try:
                    _mission_advance_fire(t["id"])
                except Exception as e:
                    _util._wlog(f"{t['id']}: mission-advance: {e!r}")
                # A scheduled task that fails would otherwise fail again
                # tomorrow, silently — the result only sits in the Tasks tab.
                # One push per DISTINCT failure text (not one per day).
                if sched and not ok and res != t.get("result"):
                    try:
                        _notify.notify_add(t.get("instance") or "task",
                                   f"Scheduled task failed: {t['id']}",
                                   (str(t.get("message", ""))[:120] + " — " + str(res))[:900],
                                   link="tasks")
                    except Exception as e:
                        _util._wlog(f"{t['id']}: failure notify: {e!r}")
                ran = True
        except Exception as e:
            _util._wlog(f"worker-loop: {e!r}")
        if not ran:
            time.sleep(5)
            # Orphan watch: if a task hangs on "running" for more than 30 min,
            # its run is lost (the timeout is 10 min) -> reset it.
            try:
                cut = int(time.time()) - 1800

                def orphan_mut(tasks2):
                    hit = []
                    for t2 in tasks2:
                        if t2.get("status") == "running" and t2.get("updated", 0) < cut:
                            t2["status"] = "scheduled" if t2.get("schedule") else "pending"
                            hit.append(t2.get("id"))
                    return bool(hit), hit
                for tid in _store.with_tasks(orphan_mut):
                    _util._wlog(f"{tid}: running orphan reset")
            except Exception as e:
                _util._wlog(f"orphan-watch: {e!r}")
            # TTL sweep while idle, at most once per hour.
            now = time.time()
            if now - _mi_sweep_ts[0] > 3600:
                _mi_sweep_ts[0] = now
                try:
                    _missions.mission_ttl_sweep()
                except Exception as e:
                    _util._wlog(f"mission-ttl-sweep failed: {e!r}")
                try:
                    task_target_sweep()
                except Exception as e:
                    _util._wlog(f"task-target-sweep failed: {e!r}")
                try:
                    _memfs.sweep([i["name"] for i in _instances.load_instances() if uses_harness(i)])
                except Exception as e:
                    _util._wlog(f"memfs-sweep failed: {e!r}")
                try:
                    _store.turns_prune(30)          # traces older than the weekly digest's reach
                except Exception as e:
                    _util._wlog(f"turns-prune failed: {e!r}")
            try:
                image_sweep()          # one stat per base image, every idle cycle
            except Exception as e:
                _util._wlog(f"image-sweep failed: {e!r}")


AGENT_TOOLS_CATALOG = [
    {"name": "bash", "desc": "Run shell commands in the workspace"},
    {"name": "read_file", "desc": "Read a file"},
    {"name": "write_file", "desc": "Write a file"},
    {"name": "write_xlsx", "desc": "Write a spreadsheet (.xlsx) into the workspace"},
    {"name": "write_docx", "desc": "Write a Word document (.docx) into the workspace"},
    {"name": "list_dir", "desc": "List a directory"},
    {"name": "offload_read", "desc": "Re-read offloaded (truncated) tool output"},
    {"name": "http_fetch", "desc": "Fetch a URL (HTTP)"},
    {"name": "read_pdf", "desc": "Extract PDF text (file or URL)"},
    {"name": "web_search", "desc": "Web search (DuckDuckGo) — needs internet"},
    {"name": "spawn_subagent", "desc": "Start an ephemeral subagent"},
    {"name": "create_task", "desc": "Queue a task (capable instance or ephemeral)"},
    {"name": "read_inbox", "desc": "Read new user messages (Signal/app/web)"},
    {"name": "list_tasks", "desc": "List running/scheduled tasks with IDs"},
    {"name": "delete_task", "desc": "Delete a running/scheduled task by ID"},
    {"name": "edit_task", "desc": "Change a task's message/schedule by ID"},
    {"name": "mission_start", "desc": "Create a mission: goal + steps (orchestrator only)"},
    {"name": "missions", "desc": "List open missions with status (orchestrator only)"},
    {"name": "mission_update", "desc": "Advance a mission step (orchestrator only)"},
    {"name": "mission_finish", "desc": "Complete a mission (orchestrator only)"},
    {"name": "send_signal", "desc": "Send a Signal message to the user (allowed numbers only)"},
    {"name": "notify", "desc": "Push notification to app + web manager (title + text)"},
    {"name": "ha_control", "desc": "Turn a Home Assistant device/area on or off by spoken name (matches + auto-learns aliases)"},
    {"name": "ha_learn_alias", "desc": "Teach Home Assistant a spoken-name alias for an entity (STT mishears names)"},
    {"name": "oracle", "desc": "Second opinion before risky actions (challenges assumptions, never acts)"},
    {"name": "list_agents", "desc": "Available agents + capabilities (routing)"},
    {"name": "recall_tasks", "desc": "Query earlier tasks/results (institutional knowledge)"},
    {"name": "list_skills", "desc": "List available skills"},
    {"name": "search_sessions", "desc": "Full-text search over earlier chats and task results"},
    {"name": "propose_skill", "desc": "Propose a reusable procedure as a skill (waits for approval)"},
    {"name": "load_skill", "desc": "Load a skill into the context"},
    {"name": "memory_store", "desc": "Remember a value permanently"},
    {"name": "memory_recall", "desc": "Retrieve a remembered value"},
    {"name": "memory_reflect", "desc": "Ask the second memory (Hindsight) a question over everything it has seen"},
    {"name": "playbook_add", "desc": "Record a permanent rule/playbook (always applies)"},
    {"name": "playbooks", "desc": "List playbooks (fixed rules)"},
    {"name": "playbook_forget", "desc": "Remove a playbook by ID"},
    {"name": "remote_ls", "desc": "List a katfs share"},
    {"name": "remote_read", "desc": "Read a katfs file"},
    {"name": "remote_write", "desc": "Write a katfs file"},
    {"name": "remote_delete", "desc": "Delete a katfs file/folder"},
    {"name": "list_secrets", "desc": "Show granted secret names"},
    {"name": "get_secret", "desc": "Fetch a granted secret"},
]
AGENT_TOOL_NAMES = {t["name"] for t in AGENT_TOOLS_CATALOG}


def create_instance(name, template, config=None, mounts=None, internet=True):
    name = "".join(c for c in name if c.isalnum() or c in "-_").lower()
    if not name:
        return "invalid name"
    if any(i["name"] == name for i in _instances.load_instances()):
        return f"'{name}' already exists"
    tpl = next((t for t in _instances.load_templates() if t.get("template") == template), None)
    if not tpl:
        return f"unknown template '{template}'"
    # defaults from template.params, overridden by the passed config,
    # empty values pre-filled from the shared settings
    cfg = {p["key"]: p.get("default", "") for p in tpl.get("params", [])}
    cfg.update({k: v for k, v in (config or {}).items() if v != ""})
    settings = _settings.load_settings()
    for k in list(cfg):
        if cfg[k] == "" and settings.get(k):
            cfg[k] = settings[k]
    for k in _settings.NEVER_PERSIST:
        cfg.pop(k, None)
    inst = {"name": name, "index": _instances.next_index(), "vcpus": tpl.get("vcpus", 2),
            "mem_mib": tpl.get("mem_mib", 1024), "rootfs": tpl["rootfs"],
            "internet": bool(internet),
            "description": f"{tpl.get('description','')} ({cfg.get('TRANSPORT','signal')}"
                           + (f", {cfg.get('FABRIC_MODEL')}" if cfg.get("FABRIC_MODEL") else "") + ")",
            "template": template, "config": cfg}
    clean = [{"host": str(m.get("host", "")).strip(),
              "guest": str(m.get("guest", "")).strip(),
              "readonly": bool(m.get("readonly"))}
             for m in (mounts or []) if isinstance(m, dict) and m.get("host") and m.get("guest")]
    if clean:
        inst["mounts"] = clean
    _instances.save_instance(inst)
    return f"instance '{name}' created from template '{template}'"


def set_instance_tools(name, tools):
    """Set an instance's tool allowlist. Empty/all list -> drop the field
    (= all tools). Takes effect at the next start (env-based)."""
    inst = next((i for i in _instances.load_instances() if i["name"] == name), None)
    if not inst:
        return "unknown"
    sel = [t for t in (tools or []) if t in AGENT_TOOL_NAMES]
    cfg = inst.setdefault("config", {})
    if sel and set(sel) != AGENT_TOOL_NAMES:
        cfg["AGENT_TOOLS"] = ",".join(sorted(sel))
    else:
        cfg.pop("AGENT_TOOLS", None)
    _instances.save_instance(inst)
    running = " (applies after stop/start)" if _instances.is_running(inst) else ""
    return f"tools for '{name}' saved{running}"


# Order = display logic in render()/list_agents: the first present key is the
# instance's model.
MODEL_KEYS = ("OPENROUTER_MODEL", "ORCAROUTER_MODEL", "ANTHROPIC_MODEL", "PI_MODEL", "PRIME_MODEL", "LLAMA_MODEL")
# For switching provider via set_model("provider:model"): provider name -> key.
PROVIDER_MODEL_KEY = {"openrouter": "OPENROUTER_MODEL", "orcarouter": "ORCAROUTER_MODEL",
                      "anthropic": "ANTHROPIC_MODEL", "pi": "PI_MODEL",
                      "prime": "PRIME_MODEL", "llama": "LLAMA_MODEL"}


def set_model(name, model):
    """Switch an existing instance's model. Sets exactly the key the instance
    already uses (no new one is invented — otherwise nobody would know which
    provider is meant). Takes effect at the next start (env-based), like the
    tool allowlist."""
    inst = next((i for i in _instances.load_instances() if i["name"] == name), None)
    if not inst:
        return "unknown"
    model = str(model or "").strip()
    if not model:
        return "error: no model given"
    cfg = inst.setdefault("config", {})
    # Provider switch: "orcarouter:tencent/hy3" additionally switches the backend
    # (sets its MODEL_KEY, removes the others). Without a prefix it stays with
    # the existing provider — only the model changes. The colon test triggers
    # ONLY for a known provider name, so ":free" model variants
    # (e.g. "mistralai/...:free") are not misread as a provider.
    if ":" in model and model.split(":", 1)[0] in PROVIDER_MODEL_KEY:
        prov, mdl = model.split(":", 1)
        key = PROVIDER_MODEL_KEY[prov]
        for k in MODEL_KEYS:
            cfg.pop(k, None)
        cfg[key] = mdl.strip()
        model = mdl.strip()
    else:
        key = next((k for k in MODEL_KEYS if k in cfg), None)
        if key is None:
            return (f"error: instance '{name}' has no model setting "
                    f"({'/'.join(MODEL_KEYS)})")
        cfg[key] = model
    _instances.save_instance(inst)
    running = " (applies after stop/start)" if _instances.is_running(inst) else ""
    return f"model for '{name}' set to {model}{running}"


def set_internet(name, on):
    inst = next((i for i in _instances.load_instances() if i["name"] == name), None)
    if not inst:
        return "unknown"
    inst["internet"] = bool(on)
    _instances.save_instance(inst)
    if _instances.is_running(inst):
        apply_internet(inst, on)   # takes effect immediately, no restart needed
    return f"internet for '{name}': {'on' if on else 'off'}"


def delete_instance(name):
    inst = next((i for i in _instances.load_instances() if i["name"] == name), None)
    if not inst:
        return "unknown"
    if _instances.is_running(inst):
        stop(inst)
    teardown_mounts(inst)   # safely remove any leftovers (binds/export)
    p = os.path.join(_paths.INST_DIR, f"{name}.json")
    if os.path.exists(p):
        os.remove(p)
    return f"instance '{name}' deleted"


# ---- Resource overview per instance (Resources tab) ------------------------
def _read_pid(inst):
    try:
        return int(open(_instances.pidfile(inst)).read().strip())
    except (OSError, ValueError):
        return None


def _proc_cpu_jiffies(pid):
    """utime+stime from /proc/<pid>/stat, robust against spaces in comm."""
    try:
        with open("/proc/%d/stat" % pid) as fh:
            after = fh.read().rpartition(")")[2].split()
        return int(after[11]) + int(after[12])   # utime (field 14) + stime (field 15)
    except (OSError, ValueError, IndexError):
        return None


def _proc_rss_kb(pid):
    try:
        with open("/proc/%d/status" % pid) as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


def resource_stats():
    """Per instance: configured size (vCPU/RAM) + live usage (RSS, CPU%,
    overlay disk). CPU% via a short sample; percentages relative to ONE core
    (a 2-vCPU guest can reach up to ~200%)."""
    insts = _instances.load_instances()
    clk = os.sysconf("SC_CLK_TCK") or 100
    pids = {i["name"]: _read_pid(i) for i in insts}
    pids = {n: p for n, p in pids.items() if p is not None and os.path.exists("/proc/%d" % p)}
    t0 = {n: _proc_cpu_jiffies(p) for n, p in pids.items()}
    dt = 0.3
    time.sleep(dt)
    t1 = {n: _proc_cpu_jiffies(p) for n, p in pids.items()}
    out = []
    for i in insts:
        name = i["name"]
        running = name in pids
        rss = _proc_rss_kb(pids[name]) if running else None
        j0, j1 = t0.get(name), t1.get(name)
        cpu_pct = round(100.0 * (j1 - j0) / (clk * dt), 1) if (j0 is not None and j1 is not None) else None
        try:
            st = os.stat(upper_path(i)); upper_used_mb = round(st.st_blocks * 512 / 1048576.0, 1)
        except OSError:
            upper_used_mb = None
        out.append({
            "name": name, "running": running,
            "vcpus": i.get("vcpus", 2), "mem_mib": i.get("mem_mib", 1024),
            "rss_mb": round(rss / 1024.0, 1) if rss else None,
            "cpu_pct": cpu_pct,
            "persist": bool(i.get("persist_disk")),
            "upper_used_mb": upper_used_mb,
        })
    return out


# ---- networking ------------------------------------------------------------
def ensure_net_base():
    _util.sh("sysctl", "-w", "net.ipv4.ip_forward=1", check=False)
    r = _util.sh("iptables", "-t", "nat", "-C", "POSTROUTING", "-s", POOL, "-o", _host.HOSTIF,
           "-j", "MASQUERADE", check=False)
    if r.returncode != 0:
        _util.sh("iptables", "-t", "nat", "-A", "POSTROUTING", "-s", POOL, "-o", _host.HOSTIF,
           "-j", "MASQUERADE", check=False)
    # Guest isolation: microVMs must NOT route to each other. A compromised
    # agent could otherwise reach another instance's chat/term ports (8080/7682,
    # bound to 0.0.0.0, no auth). Backstop DROP for pool->pool; the tap ACCEPTs
    # below are additionally scoped so they never even match guest-to-guest.
    # Guest->gateway (8700 broker) is host-local (INPUT) and unaffected by this.
    if _util.sh("iptables", "-C", "FORWARD", "-s", POOL, "-d", POOL, "-j", "DROP",
          check=False).returncode != 0:
        _util.sh("iptables", "-A", "FORWARD", "-s", POOL, "-d", POOL, "-j", "DROP", check=False)
    ensure_guest_input_rules()


# Guest -> host: what a VM legitimately needs from its gateway (.1 of the /30).
GUEST_INPUT_ACCEPT = (
    ("-p", "tcp", "--dport", str(_host.LISTEN[1])),          # manager: API, broker, LLM proxy
    ("-p", "tcp", "--dport", "2049"),                  # NFS workspace
    ("-p", "icmp"),                                    # ping the gateway
    ("-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED"),  # replies to host->guest (proxy)
)


def ensure_guest_input_rules():
    """Guest -> host is limited to the manager port and NFS. Without this every
    host service listening on 0.0.0.0 (sshd, rpcbind, …) is one hop away from
    each VM. The DROP goes in first so the ACCEPTs inserted afterwards sit
    above it; idempotent, so a restart adds nothing twice."""
    if _util.sh("iptables", "-C", "INPUT", "-i", "fc+", "-j", "DROP", check=False).returncode != 0:
        _util.sh("iptables", "-I", "INPUT", "1", "-i", "fc+", "-j", "DROP", check=False)
    for spec in GUEST_INPUT_ACCEPT:
        # Position matters, not just presence: an ACCEPT appended BELOW the
        # DROP (an older setup script did that for NFS) never matches, and a
        # presence check would leave it there. Remove every copy, insert on top.
        for _ in range(8):
            if _util.sh("iptables", "-D", "INPUT", "-i", "fc+", *spec, "-j", "ACCEPT", check=False).returncode != 0:
                break
        _util.sh("iptables", "-I", "INPUT", "1", "-i", "fc+", *spec, "-j", "ACCEPT", check=False)
    # A pool address arriving on the LAN interface is forged (a LAN box posing
    # as a stopped VM would pass every by-IP check): drop it first.
    if _host.HOSTIF and _util.sh("iptables", "-C", "INPUT", "-i", _host.HOSTIF, "-s", POOL, "-j", "DROP",
                     check=False).returncode != 0:
        _util.sh("iptables", "-I", "INPUT", "1", "-i", _host.HOSTIF, "-s", POOL, "-j", "DROP", check=False)


def _antispoof_rules(n):
    return [(chain, ("-i", n["tap"], "!", "-s", n["guest"], "-j", "DROP"))
            for chain in ("INPUT", "FORWARD")]


def ensure_antispoof(inst):
    """A VM's packets must carry its own /30 address: the source IP is the
    guest's identity for the manager (instance_by_ip), so a forged source would
    be a forged identity. Always on top — above the instance's FORWARD chain."""
    for chain, spec in _antispoof_rules(_instances.net_of(inst)):
        while _util.sh("iptables", "-C", chain, *spec, check=False).returncode == 0:
            _util.sh("iptables", "-D", chain, *spec, check=False)
        _util.sh("iptables", "-I", chain, "1", *spec, check=False)


def clear_antispoof(inst):
    for chain, spec in _antispoof_rules(_instances.net_of(inst)):
        while _util.sh("iptables", "-C", chain, *spec, check=False).returncode == 0:
            _util.sh("iptables", "-D", chain, *spec, check=False)


def setup_tap(inst):
    n = _instances.net_of(inst)
    _util.sh("ip", "link", "del", n["tap"], check=False)
    _util.sh("ip", "tuntap", "add", n["tap"], "mode", "tap")
    _util.sh("ip", "addr", "add", f"{n['host']}/30", "dev", n["tap"])
    _util.sh("ip", "link", "set", n["tap"], "up")
    # The host has FORWARD policy DROP + Docker chains in front of it -> generic
    # rules don't apply reliably. So allow tap traffic RIGHT AT THE TOP (before
    # DROP/Docker) — but ONLY to/from outside the pool. This lets the guest reach
    # the internet (destination not in the pool) and replies back (source not in
    # the pool), while guest-to-guest (both in the pool) matches no ACCEPT rule
    # and gets caught by the pool->pool DROP or the DROP policy.
    # Clear old, unrestricted ACCEPTs of the same tap first (the tap name is
    # reused on restart, otherwise the old hole would stay open).
    for spec in (["-i", n["tap"]], ["-o", n["tap"]]):
        while _util.sh("iptables", "-C", "FORWARD", *spec, "-j", "ACCEPT", check=False).returncode == 0:
            _util.sh("iptables", "-D", "FORWARD", *spec, "-j", "ACCEPT", check=False)
    apply_internet(inst, inst.get("internet", True))


# Until now, guests with internet=on could go anywhere — including the whole
# LAN. Home Assistant and Portainer were thus reachable from EVERY VM, whether
# the MCP was assigned to it or not (the broker protects the tokens, but the
# door stood open anyway). Now: internet yes, LAN no — except the endpoints of
# the MCPs listed in the instance's MCP_SERVERS, and the guests' DNS.
# DNS for the guests (ends up in resolv.conf via guest-init). Site-specific —
# set it via env on other installations; 1.1.1.1 works everywhere.
GUEST_DNS = os.environ.get("GUEST_DNS") or _settings.SITE.get("GUEST_DNS") or "1.1.1.1"
_PRIVATE_NETS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                 "100.64.0.0/10", "169.254.0.0/16")     # CGNAT/Tailscale, link-local too


def _mcp_endpoints(inst):
    """LAN targets (ip, port) that this instance needs according to MCP_SERVERS.
    Read from the catalog, not from the instance — the latter only holds names.
    IP literals only: a hostname in the catalog that resolves into the LAN would
    NOT be allowed here (deliberately; enter the IP instead)."""
    names = {x for x in (inst.get("config", {}).get("MCP_SERVERS", "") or "").split(",") if x}
    if not names:
        return []
    out = []
    for m in _mcp.load_mcps():
        if m.get("name") not in names:
            continue
        for scheme, host, port in re.findall(
                r"(https?)://(\d{1,3}(?:\.\d{1,3}){3})(?::(\d+))?", json.dumps(m)):
            try:
                import ipaddress
                if not ipaddress.ip_address(host).is_private:
                    continue          # public targets are covered by the internet rule
            except ValueError:
                continue
            out.append((host, int(port or (443 if scheme == "https" else 80))))
    return sorted(set(out))


def _llama_endpoint(inst):
    """(ip, port) of the llama.cpp server, if the instance uses it AND it is on
    the private network — then the gating must let it through. An endpoint on the
    host (reachable via the gateway) or on the internet needs no special rule."""
    ep = (inst.get("config", {}).get("LLAMA_ENDPOINT") or "").strip()
    if not ep:
        return None
    m = re.search(r"(https?)://(\d{1,3}(?:\.\d{1,3}){3})(?::(\d+))?", ep)
    if not m:
        return None
    import ipaddress
    scheme, host, port = m.group(1), m.group(2), m.group(3)
    try:
        if not ipaddress.ip_address(host).is_private:
            return None
    except ValueError:
        return None
    return (host, int(port or (443 if scheme == "https" else 80)))


def _fc_chain(inst):
    return "FC-" + re.sub(r"[^a-zA-Z0-9_.-]", "", inst["name"])[:24]


def apply_internet(inst, allow):
    """Set/remove the instance's egress rules. `allow=False` means: the VM may
    not leave its own /30 — no LAN, no internet. The manager broker at the
    gateway (8700) stays reachable (host-local, INPUT). And with it the LLM
    endpoint: an agent without internet CANNOT think.

    With allow=True the instance gets its own FORWARD chain:
      1. its MCP endpoints (tcp, targeted)     -> ACCEPT
      2. the guest DNS (53)                     -> ACCEPT
      3. private networks                       -> REJECT (not DROP: the
         agent should fail immediately, not run into a 30 s timeout)
      4. everything outside the pool (internet) -> ACCEPT
    The return path stays the generic rule: through NAT, replies are only
    possible for connections the guest opened itself."""
    n = _instances.net_of(inst)
    chain = _fc_chain(inst)

    # Clear out leftovers, idempotent: jump rule, chain, old direct rule.
    _util.sh("iptables", "-D", "FORWARD", "-i", n["tap"], "-j", chain, check=False)
    _util.sh("iptables", "-F", chain, check=False)
    _util.sh("iptables", "-X", chain, check=False)
    while _util.sh("iptables", "-C", "FORWARD", "-i", n["tap"], "!", "-d", POOL,
             "-j", "ACCEPT", check=False).returncode == 0:
        _util.sh("iptables", "-D", "FORWARD", "-i", n["tap"], "!", "-d", POOL,
           "-j", "ACCEPT", check=False)

    back = ["-o", n["tap"], "!", "-s", POOL]
    have_back = _util.sh("iptables", "-C", "FORWARD", *back, "-j", "ACCEPT", check=False).returncode == 0
    if not allow:
        if have_back:
            _util.sh("iptables", "-D", "FORWARD", *back, "-j", "ACCEPT", check=False)
        # Explicit, not by omission: "no network" used to rely on the FORWARD
        # policy being DROP — on a host where it is ACCEPT the switch did
        # nothing (found by a sandboxed sub-agent that curled the internet with
        # egress=none). The chain rejects everything outside the pool; the
        # manager at the gateway is INPUT, not FORWARD, and stays reachable.
        _util.sh("iptables", "-N", chain, check=False)
        _util.sh("iptables", "-A", chain, "!", "-d", POOL, "-j", "REJECT", check=False)
        _util.sh("iptables", "-I", "FORWARD", "1", "-i", n["tap"], "-j", chain, check=False)
        ensure_antispoof(inst)
        return

    _util.sh("iptables", "-N", chain, check=False)
    allow = list(_mcp_endpoints(inst))
    lp = _llama_endpoint(inst)
    if lp:
        allow.append(lp)
    for ip, port in allow:
        _util.sh("iptables", "-A", chain, "-d", ip, "-p", "tcp", "--dport", str(port),
           "-j", "ACCEPT", check=False)
    for proto in ("udp", "tcp"):
        _util.sh("iptables", "-A", chain, "-d", GUEST_DNS, "-p", proto, "--dport", "53",
           "-j", "ACCEPT", check=False)
    for net in _PRIVATE_NETS:
        _util.sh("iptables", "-A", chain, "-d", net, "-j", "REJECT", check=False)
    # Egress allowlist (guardrail): if EGRESS_ALLOW is in the instance config
    # (comma list of domains/IPs), the VM may go ONLY there — instead of
    # "everything except private". Domains are resolved at start (A records); a
    # stop/start is needed if the target's DNS changes. Empty = as before.
    egress = (inst.get("config", {}).get("EGRESS_ALLOW", "") or "").strip()
    if egress:
        seen = set()
        for host in [h.strip() for h in egress.split(",") if h.strip()]:
            try:
                infos = socket.getaddrinfo(host, None, socket.AF_INET)
                ips = sorted({i[4][0] for i in infos})
            except OSError:
                print(f"[egress] {inst['name']}: '{host}' not resolvable — skipped",
                      flush=True)
                continue
            for ip in ips:
                if ip not in seen:
                    seen.add(ip)
                    _util.sh("iptables", "-A", chain, "-d", ip, "-j", "ACCEPT", check=False)
        _util.sh("iptables", "-A", chain, "!", "-d", POOL, "-j", "REJECT", check=False)
    else:
        _util.sh("iptables", "-A", chain, "!", "-d", POOL, "-j", "ACCEPT", check=False)
    _util.sh("iptables", "-I", "FORWARD", "1", "-i", n["tap"], "-j", chain, check=False)
    if not have_back:
        _util.sh("iptables", "-I", "FORWARD", "1", *back, "-j", "ACCEPT", check=False)
    ensure_antispoof(inst)


def teardown_tap(inst):
    # Rules point at the tap NAME and survive deletion of the device — without
    # cleanup, dead chains pile up.
    n = _instances.net_of(inst)
    chain = _fc_chain(inst)
    _util.sh("iptables", "-D", "FORWARD", "-i", n["tap"], "-j", chain, check=False)
    _util.sh("iptables", "-F", chain, check=False)
    _util.sh("iptables", "-X", chain, check=False)
    clear_antispoof(inst)
    _util.sh("ip", "link", "del", n["tap"], check=False)


# ---- host folders (NFS bind-mounts) ----------------------------------------
def retire_root_export():
    """Remove the old pool-wide workspace export (kept as .bak once). With it
    gone, a VM reaches exactly the folders exported to its own address."""
    if not os.path.exists(AGENT_EXPORTS):
        return False
    try:
        if not os.path.exists(AGENT_EXPORTS + ".bak"):
            os.replace(AGENT_EXPORTS, AGENT_EXPORTS + ".bak")
        else:
            os.remove(AGENT_EXPORTS)
        _util.sh("exportfs", "-ra", check=False)
        print(f"[nfs] retired the pool-wide export {AGENT_EXPORTS}", flush=True)
        return True
    except OSError as e:
        print(f"[quiet] retiring {AGENT_EXPORTS} failed: {e!r}", flush=True)
        return False


def mount_specs(inst):
    """Normalized host-folder mounts: bind target, NFS subpath, fsid, mode."""
    specs = []
    for j, m in enumerate(inst.get("mounts", []) or []):
        host = str(m.get("host", "")).strip()
        guest = str(m.get("guest", "")).strip()
        if not host or not guest:
            continue
        target = os.path.join(FCMNT_ROOT, inst["name"], str(j))
        specs.append({
            "idx": j, "host": host, "guest": guest,
            "ro": bool(m.get("readonly", False)),
            "target": target, "sub": target,        # NFSv4 pseudo-root: absolute path
            "fsid": 4000 + (inst.get("index", 0) % 200) * 16 + (j % 14),
        })
    # The instance's memory folder (mgr/memfs.py) rides the same mechanism:
    # exported to this guest only, mounted read-write at /memory. Slot 15 of
    # the fsid block is reserved for it, 14 for the workspace (user mounts 0..13).
    mem = _memfs.folder(inst["name"]) if uses_harness(inst) else None
    if mem:
        target = os.path.join(FCMNT_ROOT, inst["name"], "memory")
        specs.append({"idx": "memory", "host": mem, "guest": "/memory", "ro": False,
                      "target": target, "sub": target,
                      "fsid": 4000 + (inst.get("index", 0) % 200) * 16 + 15})
    return specs


def workspace_fsid(inst):
    return 4000 + (inst.get("index", 0) % 200) * 16 + 14


def desired_lines(inst):
    """The guest's mount list: one `sub|guest|ro|rw` line per host folder."""
    return "".join(f"{s['sub']}|{s['guest']}|{'ro' if s['ro'] else 'rw'}\n" for s in mount_specs(inst))


def write_desired(inst):
    """Write desired.list under .fcmnt/<inst>/ for the admin's eye. The guest
    does NOT read it any more: the workspace is shared by every VM, so any of
    them could have written another one's list — it asks /api/mounts (by
    source IP) instead."""
    d = os.path.join(FCMNT_ROOT, inst["name"])
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "desired.list"), "w") as f:
            for s in mount_specs(inst):
                f.write(f"{s['sub']}|{s['guest']}|{'ro' if s['ro'] else 'rw'}\n")
    except OSError as e:
        print(f"[quiet] desired.list for {inst.get('name')} failed: {e!r}", flush=True)


def setup_mounts(inst):
    """Export this instance's workspace and host folders to ITS address only."""
    ensure_guest_user()
    retire_root_export()
    n = _instances.net_of(inst)
    ws = workspace_dir(inst)
    own_guest_dir(ws)
    lines = [f"{ws} {n['guest']}({export_opts(False, workspace_fsid(inst))})\n"]
    for s in mount_specs(inst):
        if not os.path.isdir(s["host"]):
            continue  # missing host folder -> skip (do not create)
        os.makedirs(s["target"], exist_ok=True)
        _util.sh("umount", "-l", s["target"], check=False)   # release any old bind
        if _util.sh("mount", "--bind", s["host"], s["target"], check=False).returncode != 0:
            continue
        if s["ro"]:
            _util.sh("mount", "-o", "remount,ro,bind", s["target"], check=False)
        lines.append(f"{s['target']} {n['guest']}({export_opts(s['ro'], s['fsid'])})\n")
    os.makedirs(EXPORTS_D, exist_ok=True)
    with open(os.path.join(EXPORTS_D, f"fc-{inst['name']}.exports"), "w") as fh:
        fh.writelines(lines)
    _util.sh("exportfs", "-ra", check=False)
    write_desired(inst)   # the reconciler in the guest picks up the mounts


def guest_can_write(host):
    """Can the squash user write into this host folder? A read-write share of
    the operator's own folder is read-only for the agent unless the folder
    lets GUEST_USER in (chown / chmod g+w with the guest's group / o+w)."""
    try:
        st = os.stat(host)
    except OSError:
        return False
    if st.st_uid == GUEST_UID:
        return bool(st.st_mode & 0o200)
    if st.st_gid == GUEST_GID:
        return bool(st.st_mode & 0o020)
    return bool(st.st_mode & 0o002)


def teardown_mounts(inst):
    ef = os.path.join(EXPORTS_D, f"fc-{inst['name']}.exports")
    if os.path.exists(ef):
        os.remove(ef)
        _util.sh("exportfs", "-ra", check=False)
    d = os.path.join(FCMNT_ROOT, inst["name"])
    if os.path.isdir(d):
        # scan the actual contents (robust against leftovers): release
        # sub-binds, then remove empty directories (rmdir fails on busy/mount).
        for sub in os.listdir(d):
            p = os.path.join(d, sub)
            if os.path.isdir(p):
                _util.sh("umount", "-l", p, check=False)
        try:
            os.remove(os.path.join(d, "desired.list"))
        except OSError:
            pass
        for sub in os.listdir(d):
            try:
                os.rmdir(os.path.join(d, sub))
            except OSError:
                pass
        try:
            os.rmdir(d)
        except OSError:
            pass


_GUEST_MOUNT_DENY = ("/bin", "/sbin", "/usr", "/lib", "/lib32", "/lib64", "/etc", "/proc", "/sys",
                     "/dev", "/boot", "/run", "/var", "/app", "/harness", "/config", "/memory",
                     "/init", "/root", "/tmp")


def _protected_host_paths():
    out = [_paths.BASE, "/etc", "/root", "/var", "/usr", "/boot"]
    if AGENT_SRC:
        out.append(AGENT_SRC)                       # a VM writing agent.py = code in every VM
    for home in glob.glob("/home/*"):
        out += [os.path.join(home, d) for d in (".config", ".ssh", ".gnupg", ".claude")]
    return [os.path.realpath(p) for p in out]


def mount_error(host, guest):
    """'' when this host folder may be shared at this guest path, else why not.
    Host side: an existing directory under BROWSE_ROOTS that neither contains
    nor lies in the manager tree, the agent sources, ~/.config, ~/.ssh — in
    the VM the folder is uid 1000, on the host that is the owner of all of it.
    Guest side: an absolute path outside the system directories — a folder
    mounted over /bin runs the sharer's files as root at the next shell."""
    hp = os.path.realpath(str(host or ""))
    if not host or not os.path.isdir(hp):
        return f"host folder {host!r} is not a directory"
    if not any(hp == r or hp.startswith(r.rstrip("/") + "/") for r in _browse.BROWSE_ROOTS):
        return f"host folder must be under {', '.join(_browse.BROWSE_ROOTS)}"
    for prot in _protected_host_paths():
        if hp == prot or hp.startswith(prot + "/") or prot.startswith(hp + "/"):
            return f"host folder {host} would expose {prot}"
    g = str(guest or "")
    if not g.startswith("/") or g.rstrip("/") == "" or "/../" in g + "/" or "\n" in g:
        return f"guest path {g!r} must be an absolute path"
    gn = os.path.normpath(g)
    if any(gn == d or gn.startswith(d + "/") for d in _GUEST_MOUNT_DENY):
        return f"guest path {g} is a system directory"
    return ""


def set_mounts(name, mounts):
    inst = next((i for i in _instances.load_instances() if i["name"] == name), None)
    if not inst:
        return "unknown"
    old_specs = mount_specs(inst)
    wanted = [{"host": str(m.get("host", "")).strip(),
               "guest": str(m.get("guest", "")).strip(),
               "readonly": bool(m.get("readonly"))}
              for m in (mounts or [])
              if isinstance(m, dict) and m.get("host") and m.get("guest")]
    for m in wanted:
        why = mount_error(m["host"], m["guest"])
        if why:
            return f"error: {why}"
    inst["mounts"] = wanted
    ensure_guest_user()
    warn = [m["host"] for m in wanted if not m["readonly"] and not guest_can_write(m["host"])]
    _instances.save_instance(inst)
    note = ""
    if _instances.is_running(inst):
        # apply LIVE: tear down removed folders, export the current (new) ones.
        new_subs = {s["sub"] for s in mount_specs(inst)}
        for s in old_specs:
            if s["sub"] not in new_subs:
                _util.sh("umount", "-l", s["target"], check=False)
                try:
                    os.rmdir(s["target"])
                except OSError:
                    pass
        setup_mounts(inst)            # bind+export of the current folders (idempotent)
        write_desired(inst)           # the running guest mounts them itself (reconciler)
        note = " (applied live)"
    if warn:
        note += (f" — read-only for the agent until {GUEST_USER} may write there: "
                 + ", ".join(warn))
    return f"{len(inst['mounts'])} host folders saved{note}"


# ---- firecracker lifecycle -------------------------------------------------
def guest_env(inst):
    """What the guest reads from config.env: the instance's config plus what
    the manager adds for this boot (paths, zone, DNS, exports)."""
    cfg = dict(inst.get("config", {}))
    # Second guard: older instance JSONs may still contain a key/MCP_CONFIG;
    # they still must not reach the disk.
    for k in _settings.NEVER_PERSIST:
        cfg.pop(k, None)
    # A minimal VM init does not have /usr/local/bin in PATH -> inject it so
    # claude/fabric are found (guest-init sources the config disk).
    cfg.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    cfg["FC_INSTANCE"] = inst["name"]   # for the host-folder reconciler in the guest
    cfg.setdefault("TZ", _host.HOST_TZ)        # the agent's clock: [Now] line per turn
    cfg["GUEST_DNS"] = GUEST_DNS         # guest-init writes resolv.conf from it (site.json, not the image)
    cfg["AGENT_EXPORT"] = workspace_dir(inst)   # its own workspace export, by absolute path
    if uses_harness(inst):
        cfg["MEMORY_DIR"] = "/memory"    # Markdown memory folder (mgr/memfs.py)
    if inst["name"] == ORCH_INSTANCE:   # only the orchestrator may manage tasks
        cfg["TASK_ADMIN"] = "1"
    # Key injection proxy active? Then the agent sends chat requests to the
    # manager instead of directly to the router — so the VM never sees an LLM key
    # (not even via the secret broker). The switch lives in the shared settings
    # so that ALL instances are switched over consistently.
    if _settings.load_settings().get("LLM_KEY_PROXY") == "1":
        cfg["KEY_PROXY"] = "1"
    return cfg


def make_config_disk(inst):
    """Create a small ext4 drive with the instance config (key=value) -> vdb."""
    cfg = guest_env(inst)
    d = os.path.join(_paths.RUN_DIR, f"{inst['name']}.cfgdir")
    os.makedirs(d, exist_ok=True)
    # Put tool plugins (firecracker/plugins/*.py) on the disk too — the agent
    # loads them at start from /config/plugins. New plugin = file + stop/start.
    pdst = os.path.join(d, "plugins")
    shutil.rmtree(pdst, ignore_errors=True)
    psrc = os.path.join(_paths.BASE, "plugins")
    if os.path.isdir(psrc):
        os.makedirs(pdst, exist_ok=True)
        for f0 in sorted(os.listdir(psrc)):
            sp = os.path.join(psrc, f0)
            if os.path.isdir(sp):                 # multi-file tool: whole folder
                shutil.copytree(sp, os.path.join(pdst, f0), dirs_exist_ok=True)
            elif f0.endswith(".py"):              # single .py (backwards compatible)
                shutil.copy2(sp, os.path.join(pdst, f0))
    with open(os.path.join(d, "config.env"), "w") as f:
        for k, v in cfg.items():
            # quote values (EXTRA_MOUNTS and others contain shell metacharacters like | and ;)
            f.write(f"{k}={shlex.quote(str(v))}\n")
    # The agent reads this disk as uid 1000: world-readable, whatever the
    # manager's umask (nothing on it is secret — NEVER_PERSIST above).
    for root, dirs, files in os.walk(d):
        os.chmod(root, 0o755)
        for f0 in files:
            os.chmod(os.path.join(root, f0), 0o644)
    img = os.path.join(_paths.RUN_DIR, f"{inst['name']}.config.ext4")
    mkfs_image(img, 16, "fcconfig", srcdir=d)
    return img


def mkfs_image(path, size_mb, label=None, srcdir=None):
    """Build an ext4 image atomically: a sparse file of size_mb, mkfs
    (populated from srcdir when given), renamed into place so a running VM
    keeps its old inode. False when mkfs fails — the old image, if any, stays."""
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".new",
                               dir=os.path.dirname(path) or ".")
    with os.fdopen(fd, "wb") as fh:
        fh.truncate(size_mb * 1024 * 1024)
    mkfs = shutil.which("mkfs.ext4", path="/usr/sbin:/sbin:" + os.environ.get("PATH", "")) or "mkfs.ext4"
    args = ["-F", "-q"] + (["-L", label] if label else []) + (["-d", srcdir] if srcdir else [])
    r = _util.sh(mkfs, *args, tmp, check=False)
    if r.returncode != 0:
        print(f"[mkfs] {os.path.basename(path)}: {r.stderr.strip()[:200]}", flush=True)
        os.unlink(tmp)
        return False
    os.replace(tmp, path)
    return True


# ---- Overlay rootfs ---------------------------------------------------------
# For images in OVERLAY_ROOTFS the VM boots with the SHARED base read-only
# (Firecracker blocks writes at the host level -> no journal conflict) plus a
# small rw upper image per instance; the guest init assembles the root from
# them via overlayfs+pivot_root. Advantage: no 2-GB copy per start, and with
# inst["persist_disk"]=true the write layer (installations!) survives a
# stop/start. Other images run unchanged via private_rootfs().
OVERLAY_ROOTFS = {"instances/openrouter-rootfs.ext4", "instances/claude-rootfs.ext4"}

# ---- Harness disk: the agent code as a read-only drive, not baked in ---------
# Pattern from Claude Code's sandbox (harness and skills are read-only shared
# layers next to the rootfs): the openrouter agent (agent.py, run_agent.py,
# webterm.py) lives on a small ext4 image the manager rebuilds from AGENT_SRC
# whenever the sources' CONTENT changes (a digest next to the image; mtimes
# lie after rsync, checkouts and clock skew), attached read-only to every VM
# on a rootfs that carries this agent. An agent change is then one instance
# restart — no docker build, no 2 GB image. The guest mounts it at /harness
# (boot arg fc_harness=/dev/vdX) and prefers it over /app; without the drive
# it boots from the rootfs as before.
AGENT_SRC = os.environ.get("AGENT_SRC") or _settings.SITE.get("AGENT_SRC") or ""
HARNESS_FILES = ("agent.py", "run_agent.py", "webterm.py")
HARNESS_IMG = os.path.join(_paths.RUN_DIR, "harness.ext4")
HARNESS_ROOTFS = {"instances/openrouter-rootfs.ext4"}     # images built from AGENT_SRC
_harness_lock = threading.Lock()


def harness_sources():
    """All HARNESS_FILES under AGENT_SRC — or nothing: a half-present set
    (agent.py mid-rename, a partial rsync) must not become the drive a VM
    boots from; run_agent.py imports agent with no fallback."""
    if not AGENT_SRC:
        return []
    ps = [os.path.join(AGENT_SRC, f) for f in HARNESS_FILES]
    return ps if all(os.path.isfile(p) for p in ps) else []


def _harness_digest(srcs):
    h = hashlib.sha256()
    for p in srcs:
        h.update(os.path.basename(p).encode() + b"\0")
        with open(p, "rb") as fh:
            h.update(fh.read())
        h.update(b"\0")
    return h.hexdigest()


def harness_image():
    """Path of the harness drive, (re)built when the sources' digest differs
    from the one recorded at the last build; None when AGENT_SRC is not
    configured or incomplete. Serialized: two starts (or the sweep and a
    start) must not build into the same file."""
    srcs = harness_sources()
    if not srcs:
        return None
    with _harness_lock:
        stamp = HARNESS_IMG + ".src"
        try:
            want = _harness_digest(srcs)
            with open(stamp) as fh:
                have = fh.read().strip()
        except OSError:
            have = ""
        if have == want and os.path.exists(HARNESS_IMG):
            return HARNESS_IMG
        d = tempfile.mkdtemp(prefix="harness-", dir=_paths.RUN_DIR)
        try:
            for p in srcs:
                shutil.copy2(p, os.path.join(d, os.path.basename(p)))
            if not mkfs_image(HARNESS_IMG, 8, "kaim56-harness", srcdir=d):
                return HARNESS_IMG if os.path.exists(HARNESS_IMG) else None
            with open(stamp, "w") as fh:
                fh.write(want)
            print(f"[harness] rebuilt from {AGENT_SRC} ({len(srcs)} files)", flush=True)
            return HARNESS_IMG
        finally:
            shutil.rmtree(d, ignore_errors=True)


def uses_harness(inst):
    """By image, not template name: llama/orcarouter share the openrouter
    rootfs and its agent, so they take (and go stale with) the same drive."""
    return inst.get("rootfs") in HARNESS_ROOTFS


def image_state(inst):
    """(stale, built, started): stale when a RUNNING VM on a shared base image
    was started before that image was last rebuilt — it still runs the old
    agent and will until stop/start. spawn_subagent was dead for three weeks
    and a tool fix missed the voice instance this way; nobody could see it."""
    if inst.get("rootfs") not in OVERLAY_ROOTFS or not _instances.is_running(inst):
        return False, 0, 0
    try:
        built = os.path.getmtime(os.path.join(_paths.BASE, inst["rootfs"]))
        if uses_harness(inst) and os.path.exists(HARNESS_IMG):
            built = max(built, os.path.getmtime(HARNESS_IMG))   # agent code counts too
        started = os.path.getmtime(_instances.pidfile(inst))
    except OSError:
        return False, 0, 0
    return started < built, built, started


def stale_instances():
    return [i["name"] for i in _instances.load_instances() if image_state(i)[0]]


_img_seen = {}      # rootfs path -> mtime last seen (filled at startup: no push for old news)


def image_sweep():
    """Idle worker: when a base image was rebuilt, push ONCE which running
    instances still sit on the old one. Stays quiet if nobody is affected."""
    hit = []
    try:
        harness_image()        # an edited agent.py shows up here, not at the next start
    except Exception as e:
        _util._wlog(f"image-sweep harness: {e!r}")
    for rel in sorted(OVERLAY_ROOTFS) + [HARNESS_IMG]:
        try:
            mt = os.path.getmtime(rel if os.path.isabs(rel) else os.path.join(_paths.BASE, rel))
        except OSError:
            continue
        if rel in _img_seen and mt > _img_seen[rel]:
            hit.append(rel)
        _img_seen[rel] = mt
    if not hit:
        return []
    old = stale_instances()
    if old:
        try:
            _notify.notify_add("rebuild", f"Rootfs rebuilt: {len(old)} instance(s) on the old image",
                       ", ".join(old) + " — restart them to pick up the new agent.",
                       link="instances")
        except Exception as e:
            _util._wlog(f"image-sweep notify: {e!r}")
    return old
UPPER_SIZE_MB = 1024          # throwaway layer per start
UPPER_PERSIST_SIZE_MB = 4096  # persistent layer (apt/pip need room); sparse


def upper_path(inst):
    if inst.get("persist_disk"):
        return os.path.join(_paths.INST_DIR, f"{inst['name']}-upper.ext4")
    return os.path.join(_paths.RUN_DIR, f"{inst['name']}.upper.ext4")


def make_upper(inst):
    """Provide an empty (or, with persist, existing) upper image."""
    p = upper_path(inst)
    if inst.get("persist_disk") and os.path.exists(p):
        return p
    size = UPPER_PERSIST_SIZE_MB if inst.get("persist_disk") else UPPER_SIZE_MB
    if not mkfs_image(p, size, "fcupper"):
        raise RuntimeError(f"mkfs of the upper layer for {inst['name']} failed")
    return p


def reset_upper(name):
    """Delete the persistent write layer (factory reset). Only while stopped."""
    inst = next((i for i in _instances.load_instances() if i["name"] == name), None)
    if not inst:
        return "unknown"
    if _instances.is_running(inst):
        return "error: instance is running — stop it first"
    n = 0
    for p in (os.path.join(_paths.INST_DIR, f"{name}-upper.ext4"),
              os.path.join(_paths.RUN_DIR, f"{name}.upper.ext4")):
        try:
            os.remove(p); n += 1
        except OSError:
            pass
    return f"disk reset ({n} layer(s) removed)" if n else "nothing to reset"


def set_persist_disk(name, on):
    inst = next((i for i in _instances.load_instances() if i["name"] == name), None)
    if not inst:
        return "unknown"
    if inst.get("rootfs") not in OVERLAY_ROOTFS:
        return "error: this template's rootfs has no overlay support (yet)"
    inst["persist_disk"] = bool(on)
    _instances.save_instance(inst)
    running = " (applies after stop/start)" if _instances.is_running(inst) else ""
    return f"persistent disk for '{name}' {'ON' if on else 'off'}{running}"


def private_rootfs(inst):
    """Create a fresh rootfs copy for exactly this VM and return its path.

    All instances of a template pointed at the SAME ext4 image, writable. Two
    VMs running simultaneously then share one journal — that worked as long as
    barely anything was written, and ended on Aug 15 with 'error loading
    journal' at boot. Hence: a separate copy per start (sparse, ~seconds fast).
    Side effect, and a deliberate one: a restart always boots the current
    template image, rootfs updates take effect as before with stop/start. State
    that should persist doesn't live here anyway, but centrally (memory.json,
    chats.json, katfs)."""
    src = os.path.join(_paths.BASE, inst["rootfs"])
    dst = os.path.join(_paths.RUN_DIR, f"{inst['name']}.rootfs.ext4")
    tmp = dst + ".new"
    # --sparse=always: the 2-GB image carries ~550 MB; the copy should occupy
    # just as little. First .new, then rename — a half copy must never start as
    # a rootfs.
    _util.sh("cp", "--sparse=always", src, tmp)
    os.replace(tmp, dst)
    return dst


def _vdev(drives):
    """Guest device of the LAST drive in the list (virtio-blk: vda, vdb, …)."""
    return f"/dev/vd{chr(ord('a') + len(drives) - 1)}"


def gen_config(inst):
    n = _instances.net_of(inst)
    boot = (f"console=ttyS0 reboot=k panic=1 pci=off "
            f"ip={n['guest']}::{n['host']}:{n['mask']}::eth0:off init=/init")
    overlay = inst.get("rootfs") in OVERLAY_ROOTFS
    if overlay:
        drives = [{"drive_id": "rootfs", "path_on_host": os.path.join(_paths.BASE, inst["rootfs"]),
                   "is_root_device": True, "is_read_only": True}]
    else:
        drives = [{"drive_id": "rootfs", "path_on_host": private_rootfs(inst),
                   "is_root_device": True, "is_read_only": False}]
    cfg_disk = os.path.join(_paths.RUN_DIR, f"{inst['name']}.config.ext4")
    if os.path.exists(cfg_disk):
        drives.append({"drive_id": "config", "path_on_host": cfg_disk,
                       "is_root_device": False, "is_read_only": True})
    for j, d in enumerate(inst.get("extra_drives", [])):
        drives.append({"drive_id": f"data{j}", "path_on_host": d["path"],
                       "is_root_device": False, "is_read_only": d.get("readonly", False)})
    if uses_harness(inst):
        himg = harness_image()
        if himg:
            drives.append({"drive_id": "harness", "path_on_host": himg,
                           "is_root_device": False, "is_read_only": True})
            boot += f" fc_harness={_vdev(drives)}"
    if overlay:
        # Last drive = upper; the device name follows from the position
        # (virtio-blk: vda, vdb, ...). The guest reads it from /proc/cmdline.
        drives.append({"drive_id": "upper", "path_on_host": make_upper(inst),
                       "is_root_device": False, "is_read_only": False})
        boot += f" fc_upper={_vdev(drives)}"
    return {
        "boot-source": {"kernel_image_path": _paths.KERNEL, "boot_args": boot},
        "drives": drives,
        "network-interfaces": [{"iface_id": "eth0", "host_dev_name": n["tap"],
                                "guest_mac": n["mac"]}],
        "machine-config": {"vcpu_count": inst.get("vcpus", 2),
                           "mem_size_mib": inst.get("mem_mib", 1024)},
    }


def start(inst):
    if _instances.is_running(inst):
        return "already running"
    ensure_net_base()
    setup_tap(inst)
    setup_mounts(inst)
    make_config_disk(inst)
    cfg = os.path.join(_paths.RUN_DIR, f"{inst['name']}.config.json")
    json.dump(gen_config(inst), open(cfg, "w"))
    sock = os.path.join(_paths.RUN_DIR, f"{inst['name']}.sock")
    log = open(os.path.join(_paths.RUN_DIR, f"{inst['name']}.log"), "ab")
    try:                                  # the operator may tail the console (root:operator, 0640)
        os.chmod(log.name, 0o640); os.chown(log.name, 0, _host.ADMIN_GID)
    except OSError:
        pass
    if os.path.exists(sock):
        os.remove(sock)
    p = subprocess.Popen([_paths.BIN, "--api-sock", sock, "--config-file", cfg],
                         stdout=log, stderr=log, start_new_session=True)
    open(_instances.pidfile(inst), "w").write(str(p.pid))
    return f"started (pid {p.pid})"


def stop(inst):
    pf = _instances.pidfile(inst)
    if os.path.exists(pf):
        try:
            os.kill(int(open(pf).read().strip()), signal.SIGTERM)
            time.sleep(1)
        except (ValueError, ProcessLookupError):
            pass
        os.remove(pf)
    teardown_tap(inst)
    teardown_mounts(inst)
    _mcp.mcp_hub_kill(inst["name"])
    # The private rootfs copy is worthless after stopping (the next start pulls
    # a fresh one) — just disk space, so remove it.
    for f in (f"{inst['name']}.rootfs.ext4", f"{inst['name']}.upper.ext4"):
        try:
            os.remove(os.path.join(_paths.RUN_DIR, f))
        except OSError:
            pass
    return "stopped"


# ---- Personas / system prompts --------------------------------------------
PERSONAS_FILE = os.path.join(_paths.BASE, "personas.json")
_DEFAULT_PERSONAS = [
    {"name": "assistant",
     "prompt": "You are a helpful agent with tools (shell, files, web, MCP). "
               "Use tools when needed, otherwise answer directly. Keep it brief."},
    {"name": "researcher",
     "prompt": "You are a thorough researcher. Use web_search and http_fetch, check multiple "
               "sources and cite URLs as evidence. Summarize in a structured way. For large "
               "tasks, use spawn_subagent to research sub-questions in parallel."},
    {"name": "coder",
     "prompt": "You are an experienced software developer. Use bash/read_file/write_file in the "
               "workspace, work in small steps, test your result and briefly explain what you do."},
    {"name": "translator",
     "prompt": "You are a precise translator. Translate naturally and idiomatically, without "
               "comments, unless the user explicitly asks for them."},
]


def load_personas():
    try:
        with open(PERSONAS_FILE) as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return data
    except (FileNotFoundError, ValueError):
        save_personas(_DEFAULT_PERSONAS)
        return list(_DEFAULT_PERSONAS)
    return list(_DEFAULT_PERSONAS)


def save_personas(items):
    if not isinstance(items, list):
        return -1
    try:
        with open(PERSONAS_FILE, "w") as fh:
            json.dump(items, fh, indent=2, ensure_ascii=False)
        return len(items)
    except OSError:
        return -1


def upsert_persona(name, prompt, tools=None, model=None):
    name = re.sub(r"[^a-z0-9_-]", "", (name or "").lower())
    if not name:
        return "invalid name (only a-z 0-9 _ -)"
    prev = next((p for p in load_personas() if p.get("name") == name), {})
    items = [p for p in load_personas() if p.get("name") != name]
    ent = {"name": name, "prompt": prompt or ""}
    # A persona may recommend a tool subset and a model, pre-filled when an
    # instance is created from it. None = "not provided" -> keep the existing
    # values (a web save posts only name+prompt); an explicit value replaces,
    # an explicit empty clears.
    if tools is None:
        if prev.get("tools"):
            ent["tools"] = prev["tools"]
    else:
        tl = [t.strip() for t in tools if str(t).strip()] if isinstance(tools, (list, tuple)) else \
             [t.strip() for t in str(tools).split(",") if t.strip()]
        keep = [t for t in tl if t in AGENT_TOOL_NAMES]
        if keep:
            ent["tools"] = keep
    if model is None:
        if prev.get("model"):
            ent["model"] = prev["model"]
    elif str(model).strip():
        ent["model"] = str(model).strip()[:120]
    items.append(ent)
    save_personas(items)
    return f"persona '{name}' saved"


def delete_persona(name):
    save_personas([p for p in load_personas() if p.get("name") != name])
    return f"persona '{name}' deleted"


# ---- Skills library (expert knowledge, loaded on demand by the agent) -------
SKILLS_FILE = os.path.join(_paths.BASE, "skills.json")


def load_skills():
    try:
        with open(SKILLS_FILE) as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return data
    except (FileNotFoundError, ValueError):
        pass
    return []


def save_skills(items):
    if not isinstance(items, list):
        return -1
    try:
        with open(SKILLS_FILE, "w") as fh:
            json.dump(items, fh, indent=2, ensure_ascii=False)
        return len(items)
    except OSError:
        return -1


def upsert_skill(name, description, content):
    name = re.sub(r"[^a-z0-9_-]", "", (name or "").lower())
    if not name:
        return "invalid name (only a-z 0-9 _ -)"
    items = [s for s in load_skills() if s.get("name") != name]
    items.append({"name": name, "description": description or "", "content": content or ""})
    save_skills(items)
    return f"skill '{name}' saved"


def delete_skill(name):
    save_skills([s for s in load_skills() if s.get("name") != name])
    return f"skill '{name}' deleted"


# ---- skills from experience -------------------------------------------------
# After a long, successful turn the agent distills the way it went into a
# SKILL proposal (Hermes' learning loop, with our approval gate): it lands
# here, the operator approves it in the Skills tab, only then it enters the
# catalog every agent loads from. Nothing an agent writes becomes a skill by
# itself.
SKILL_PROPOSALS_FILE = os.path.join(_paths.BASE, "skill_proposals.json")
PROPOSALS_MAX = 50
_SECRETISH = re.compile(r"(sk-or-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9]{16,}|Bearer [A-Za-z0-9._-]{16,}|hf_[A-Za-z0-9]{16,})")


def load_proposals():
    try:
        with open(SKILL_PROPOSALS_FILE) as fh:
            d = json.load(fh)
        return d if isinstance(d, list) else []
    except (FileNotFoundError, ValueError):
        return []


def save_proposals(items):
    tmp = SKILL_PROPOSALS_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(items, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, SKILL_PROPOSALS_FILE)


def skill_lint(name, description, content):
    """'' when a proposal is acceptable, else why not. Advisory rules in the
    spirit of Hermes' linter: a usable name, a one-line description, a body
    that is a procedure and not a dump, nothing that looks like a credential."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,47}", name or ""):
        return "name must be 2-48 chars of a-z 0-9 _ -"
    if not (description or "").strip() or len(description) > 200:
        return "description must be one line (1-200 chars)"
    body = (content or "").strip()
    if len(body) < 80:
        return "content too short to be a procedure"
    if len(body) > 12000:
        return "content over 12 kB — a skill is a procedure, not a log"
    if _SECRETISH.search(body):
        return "content looks like it contains a credential"
    return ""


def proposal_add(instance, name, description, content, turn="", note=""):
    name = re.sub(r"[^a-z0-9_-]", "", str(name or "").lower())[:48]
    why = skill_lint(name, str(description or "").strip(), str(content or ""))
    if why:
        return None, why
    items = [p for p in load_proposals() if not (p.get("name") == name and p.get("status") == "proposed")]
    pid = uuid.uuid4().hex[:10]
    items.append({"id": pid, "ts": int(time.time()), "instance": str(instance or "")[:80], "turn": str(turn or "")[:16],
                  "name": name, "description": str(description).strip()[:200], "content": str(content).strip(),
                  "note": str(note or "")[:200], "status": "proposed",
                  "update": any(s.get("name") == name for s in load_skills())})
    items = items[-PROPOSALS_MAX:]
    save_proposals(items)
    try:
        _notify.notify_add(instance or "skills", f"Skill proposal: {name}",
                   f"{str(description).strip()[:160]} — review in the Skills tab", link="")
    except Exception:
        pass
    return pid, "ok"


def proposal_decide(pid, approve):
    items = load_proposals()
    p = next((x for x in items if x.get("id") == pid), None)
    if p is None:
        return "unknown"
    if approve:
        msg = upsert_skill(p["name"], p.get("description", ""), p.get("content", ""))
        p["status"] = "approved"
    else:
        msg = f"proposal '{p['name']}' discarded"
        p["status"] = "discarded"
    p["decided"] = int(time.time())
    save_proposals(items)
    return msg


def sessions_search(query, instance=None, limit=10):
    """Exact search over chats and task runs (FTS5), index refreshed from
    chats.json when it changed."""
    try:
        mt = os.path.getmtime(CHATS_FILE)
    except OSError:
        mt = 0
    _store.sessions_refresh(load_chats(), mt)
    return _store.sessions_query(query, instance=instance, limit=limit)


# ---- Playbooks + prompt templates: moved out to mgr/rules.py ---------------
from mgr import rules as _rules  # noqa: E402
_rules.configure(_paths.BASE)


# ---- Missions: moved out to mgr/missions.py (imported early, see above) ----


def instance_by_ip(ip):
    for i in _instances.load_instances():
        try:
            if _instances.net_of(i).get("guest") == ip:
                return i
        except Exception:
            continue
    return None


# ---- MCP catalog + hub: moved out to mgr/mcp.py ----------------------------


# ---- katfs: moved out to mgr/katfs.py --------------------------------------

from mgr import irohgw as _irohgw  # noqa: E402
_irohgw.configure(_paths.BASE)

def tool_allowed(inst, name):
    """A-2: enforce the per-instance AGENT_TOOLS allowlist at the HOST, not only
    in the guest. Empty allowlist = all tools (no behaviour change for the many
    instances that set none). A restricted instance is refused a capability it
    did not list — the allowlist becomes a real boundary, not a display hint."""
    at = ((inst or {}).get("config") or {}).get("AGENT_TOOLS", "")
    allow = {t.strip() for t in at.split(",") if t.strip()}
    return (not allow) or name in allow


def effective_policy(inst):
    """Everything an instance IS ALLOWED to do in one place: network, tools,
    secrets, MCP servers, model. Pulls the scattered controls (instance config,
    secret-policy) together into one view."""
    cfg = inst.get("config") or {}
    at = cfg.get("AGENT_TOOLS", "")
    tools_allowed = [t.strip() for t in at.split(",") if t.strip()] if at else None  # None = all
    model = cfg.get("OPENROUTER_MODEL") or cfg.get("PI_MODEL") or cfg.get("PRIME_MODEL") or ""
    mcps = [n for n in (cfg.get("MCP_SERVERS", "") or "").split(",") if n]
    return {
        "name": inst["name"],
        "template": inst.get("template", ""),
        "running": _instances.is_running(inst),
        "internet": inst.get("internet", True),
        "model": model,
        "tools_all": tools_allowed is None,
        "tools": tools_allowed if tools_allowed is not None else [t["name"] for t in AGENT_TOOLS_CATALOG],
        "secrets": sorted(_secrets.allowed_secret_keys(inst)),
        "mcps": mcps,
        "katfs_share": cfg.get("KATFS_SHARE", ""),
        "auto_reset": str(cfg.get("AUTO_RESET_MIN", "") or "0"),
    }


# mgr/mcp needs the secret functions; they are defined above by now.
_mcp.configure(_paths.BASE, _instances.load_instances, _secrets.allowed_secret_keys, _secrets.secret_store)

# ---- web -------------------------------------------------------------------
# PAGE (HTML/JS of the manager UI) now lives in mgr/ui.py.

# ---- Brand ------------------------------------------------------------------
# logo.svg is kept as a file (favicon, shared elsewhere). For the header mark
# the navy inherits the text color so it carries in both the light and the dark
# theme; the turquoise stays the accent.
BRAND = "kAIm56"
LOGO_FILE = os.path.join(_paths.BASE, "logo.svg")
try:
    with open(LOGO_FILE) as _fh:
        LOGO_SVG = _fh.read()
except OSError:
    LOGO_SVG = ""
LOGO_INLINE = (LOGO_SVG.replace("#1D2A4D", "currentColor")
                       .replace('width="512" height="512"',
                                'width="26" height="26" class=mark')
                       .replace("\n", "").strip())

# Icons for the server-side rendered instance rows (Feather style, 14px).
_SVG = ('<svg width=14 height=14 viewBox="0 0 24 24" fill=none stroke=currentColor '
        'stroke-width=1.5 stroke-linecap=round stroke-linejoin=round>%s</svg>')
IC_TERM = _SVG % '<polyline points="4 17 10 11 4 5"></polyline><line x1=12 y1=19 x2=20 y2=19></line>'
IC_CHAT = _SVG % '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>'
IC_FILES = _SVG % ('<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9'
                   'A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"></path>')
IC_DEL = _SVG % ('<path d="M3 6h18"></path><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"></path>'
                 '<path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>')
# small folder icon (12px) for the mount rows — instead of 📁 (emoji tofus without an emoji font)
IC_AUDIT = _SVG % ('<path d="M4 5h16M4 12h16M4 19h10"></path>'
                   '<circle cx="19" cy="19" r="2.4"></circle><path d="M22 22l-1.3-1.3"></path>')
IC_FILES2 = ('<svg width=12 height=12 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=1.6 '
             'stroke-linecap=round stroke-linejoin=round style="vertical-align:-1px"><path d="M4 20h16a2 '
             '2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 '
             '2v13a2 2 0 0 0 2 2Z"></path></svg>')


def h(v):
    """HTML escape for server-side rendering. The instance name is trimmed to
    [a-z0-9-_] on creation, but everything else comes freely from forms or
    templates — model ID (free text field), description, mount paths, tool
    list. Without escaping it lands raw in the markup: anyone who sets a mount
    row or a custom model ID would otherwise write script into the admin page."""
    return html.escape(str(v if v is not None else ""), quote=True)


def _fmt_tok(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1000:
        return f"{n/1000:.1f}k".replace(".0k", "k")
    return str(n)


def _fmt_cost(c):
    c = float(c or 0.0)
    return f"${c:.2f}" if c >= 0.01 else f"${c:.4f}"


def render():
    rows = ""
    usage = _store.usage_summary()
    for inst in _instances.load_instances():
        n = _instances.net_of(inst)
        run = _instances.is_running(inst)
        name = inst["name"]
        transport = (inst.get("config") or {}).get("TRANSPORT", "signal")
        cfgm = inst.get("config") or {}
        model = next((cfgm[k] for k in MODEL_KEYS if cfgm.get(k)), "")
        sub = " · ".join(x for x in (inst.get("template", ""), transport) if x)
        _chip = ('<svg width=12 height=12 viewBox="0 0 24 24" fill=none stroke=currentColor '
                 'stroke-width=1.6 stroke-linecap=round stroke-linejoin=round style="vertical-align:-1px">'
                 '<rect x=6 y=6 width=12 height=12 rx="1"/><path d="M9 2v2M15 2v2M9 20v2M15 20v2'
                 'M2 9h2M2 15h2M20 9h2M20 15h2"/></svg>')
        # The chip is clickable: opens the model-switch dialog (editModel in PAGE JS).
        model_line = (f"<button class='mono' style=\"font-size:12px;color:var(--color-accent-700);"
                      f"display:inline-flex;align-items:center;gap:5px;background:none;border:none;"
                      f"padding:0;cursor:pointer;text-align:left\" title=\"Change model\" "
                      f"onclick=\"editModel('{name}')\">{_chip}{h(model)}</button>"
                      if model else "")
        u = usage.get(name) or {}
        ut, ud = u.get("total") or {}, u.get("today") or {}
        usage_line = f"<span class='text-muted' style='font-size:12px' data-usage='{name}'></span>"
        if ut.get("calls"):
            usage_line = (
                f"<span class='text-muted' style='font-size:12px' data-usage='{name}' "
                f"title='LLM usage reported by this agent "
                f"({ut['calls']} calls total)'>"
                f"Tokens today {_fmt_tok(ud.get('in'))}&nbsp;/&nbsp;{_fmt_tok(ud.get('out'))}"
                f" · {_fmt_cost(ud.get('cost'))}"
                f" &nbsp;·&nbsp; total {_fmt_tok(ut['in'])}&nbsp;/&nbsp;{_fmt_tok(ut['out'])}"
                f" · {_fmt_cost(ut['cost'])}</span>")
        stale, built, started = image_state(inst)
        if run and stale:
            st = ("<span class='tag' style='background:#c0392b;color:#fff' title='started "
                  + time.strftime("%d.%m. %H:%M", time.localtime(started))
                  + ", image rebuilt " + time.strftime("%d.%m. %H:%M", time.localtime(built))
                  + " — still runs the OLD agent until restarted'>● running · old image</span>")
        else:
            st = ("<span class='tag tag-accent'>● running</span>" if run
                  else "<span class='tag tag-neutral'>○ off</span>")
        net = inst.get("internet", True)
        tools_cfg = (inst.get("config") or {}).get("AGENT_TOOLS", "")
        ntag = (f"<button class='tag {'tag-accent' if net else 'tag-neutral'}' "
                f"style='border:none;cursor:pointer' title='Toggle internet access' "
                f"onclick=\"toggleNet('{name}',{str(not net).lower()})\">"
                f"{'🌐 internet on' if net else '🚫 offline'}</button>")
        ttag = (f"<span class='tag tag-neutral' title='{h(tools_cfg)}'>🔧 {len(tools_cfg.split(','))} Tools</span>"
                if tools_cfg else "")
        ptag = ""
        if inst.get("rootfs") in OVERLAY_ROOTFS:
            pers = bool(inst.get("persist_disk"))
            ptag = (f"<button class='tag {'tag-accent' if pers else 'tag-neutral'}' "
                    f"style='border:none;cursor:pointer' "
                    f"title='Persistent disk: installations survive stop/start"
                    f"{' — right-click: reset disk' if pers else ''}' "
                    f"onclick=\"togglePersist('{name}',{str(not pers).lower()})\" "
                    f"oncontextmenu=\"return diskReset('{name}')\">"
                    f"{'💾 persistent' if pers else '↺ fresh per start'}</button>")
        btn = ""
        if run:
            btn += (f"<a href=\"/i/{name}/term/\" target=_blank class=\"btn btn-secondary btn-sm\""
                    f" title=\"Browser terminal\">{IC_TERM}Terminal</a>")
        if transport == "web":
            btn += (f"<a href=\"/chat?i={name}\" class=\"btn btn-secondary btn-sm\""
                    f" title=\"Chat with the agent\">{IC_CHAT}Chat</a>")
        btn += (f"<button class=\"btn {'btn-secondary' if run else 'btn-primary'} btn-sm\""
                f" style=\"min-width:64px\" onclick=\"act('{name}','{'stop' if run else 'start'}')\">"
                f"{'Stop' if run else 'Start'}</button>")
        if run and stale:
            btn += (f"<button class=\"btn btn-primary btn-sm\" title=\"Stop + start on the current image\""
                    f" onclick=\"act('{name}','restart')\">Restart on new image</button>")
        btn += (f"<button class=\"btn btn-icon btn-secondary\" style=\"width:32px;height:32px\""
                f" title=\"Audit / activity (tools & URLs called)\""
                f" onclick=\"openActivity('{name}')\">{IC_AUDIT}</button>")
        btn += (f"<button class=\"btn btn-icon btn-secondary\" style=\"width:32px;height:32px\""
                f" title=\"Host folders\" onclick=\"editMounts('{name}')\">{IC_FILES}</button>")
        btn += (f"<button class=\"btn btn-icon btn-secondary\" style=\"width:32px;height:32px;"
                f"color:var(--color-neutral-600)\" title=Delete onclick=\"del('{name}')\">{IC_DEL}</button>")
        mtxt = ""
        for m in inst.get("mounts", []) or []:
            mtxt += (f"<div class='text-muted' style='font-size:12px'>{IC_FILES2} {h(m.get('host'))} → "
                     f"{h(m.get('guest'))}{' (ro)' if m.get('readonly') else ''}</div>")
        rows += (f"<tr><td data-label=Instance>"
                 f"<div style='display:flex;flex-direction:column;gap:2px'>"
                 f"<span style=\"font-family:var(--font-heading);font-weight:600;font-size:16px\">{name}</span>"
                 f"<span class='text-muted' style='font-size:12px'>{h(sub)}</span>"
                 f"{model_line}"
                 f"{usage_line}"
                 f"<span class='text-muted' style='font-size:12px'>{h(inst.get('description',''))}</span>"
                 f"{mtxt}</div></td>"
                 f"<td data-label=Status><div style='display:flex;flex-direction:column;gap:4px;align-items:flex-start'>{st}{ntag} {ptag} {ttag}</div></td>"
                 f"<td data-label='vCPU / RAM' style='font-variant-numeric:tabular-nums'>"
                 f"{inst.get('vcpus',2)} / {inst.get('mem_mib',1024)} MiB</td>"
                 f"<td data-label='Guest IP' class=mono>{n['guest']}</td>"
                 f"<td data-label=Actions><div class=acts>{btn}</div></td></tr>")
    tpls = "".join(f"<option value='{h(t['template'])}'>{h(t['template'])} — {h(t.get('description',''))}</option>"
                   for t in _instances.load_templates())
    empty = ("<tr><td colspan=5 class=text-muted style='padding:18px 8px'>"
             "no instances yet — create one below</td></tr>")
    return (_ui.PAGE.replace("__LOGO__", LOGO_INLINE)
                .replace("__ROWS__", rows or empty)
                .replace("__TPLS__", tpls or "<option>no templates</option>")
                .replace("__TPLJSON__", _util.js_json(_instances.load_templates()))
                .replace("__SETTINGS__", _util.js_json(_settings.settings_for_ui()))
                .replace("__SETTINGS_SCHEMA__", _util.js_json(_settings.settings_schema()))
                .replace("__PERSONAS__", _util.js_json(load_personas(), ensure_ascii=False))
                # Only name + description into the page: with an imported
                # catalog the contents are ~1 MB, and the UI needs them only
                # when editing (then it fetches GET /api/skills/<name>).
                .replace("__SKILLS__", _util.js_json(
                    [{"name": x.get("name", ""), "description": x.get("description", "")}
                     for x in load_skills()], ensure_ascii=False))
                .replace("__HOSTIF__", _host.HOSTIF).replace("__POOL__", POOL)
                .replace("__PUBLIC_HOST__", _settings.PUBLIC_HOST)
                .replace("__SIGNAL_HOST__", _settings.SIGNAL_HOST)
                .replace("__CODE_LINK__",
                         f'<a href="{html.escape(_settings.CODE_URL, quote=True)}" target="_blank" '
                         f'rel="noopener noreferrer">VS&nbsp;Code</a>' if _settings.CODE_URL else "")
                .replace("__HOME__", os.path.expanduser(
                    "~" + (os.environ.get("SUDO_USER") or "")))
                )


# ---- Chat (UI under /chat, see chatui.py) ----------------------------------
# Chattable is every instance with TRANSPORT=web: the bridge in the microVM
# serves /api/chat (and optionally /api/chat/stream) on :8080.

def web_instances():
    """Instances you can chat with (+ running state for the UI)."""
    return [{"name": i["name"], "running": _instances.is_running(i),
             "description": i.get("description", "")}
            for i in _instances.load_instances()
            if (i.get("config") or {}).get("TRANSPORT") == "web"]


def wait_web(inst, timeout=120):
    """Starts the instance if needed and waits until the bridge accepts."""
    if not _instances.is_running(inst):
        start(inst)
    ip = _instances.net_of(inst)["guest"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection((ip, _instances.WEB_GUEST_PORT), 2).close()
            return True
        except OSError:
            time.sleep(1)
    return False


def guest_chat(inst, message, image=None, timeout=620):
    """Non-streaming call to the bridge in the microVM."""
    payload = {"message": message}
    if image:
        payload["image"] = image
    req = urllib.request.Request(
        f"http://{_instances.net_of(inst)['guest']}:{_instances.WEB_GUEST_PORT}/api/chat",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    body = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
    try:
        return json.loads(body).get("reply", body)
    except ValueError:
        return body


def guest_stream(inst, message, image, on_token, timeout=620):
    """Streams tokens from /api/chat/stream. Bridges without streaming answer on
    the same path with JSON — that then arrives as a single piece."""
    payload = {"message": message}
    if image:
        payload["image"] = image
    req = urllib.request.Request(
        f"http://{_instances.net_of(inst)['guest']}:{_instances.WEB_GUEST_PORT}/api/chat/stream",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except Exception:
        on_token(guest_chat(inst, message, image, timeout))
        return
    if "json" in (r.headers.get("Content-Type") or ""):
        body = r.read().decode("utf-8", "replace")
        try:
            on_token(json.loads(body).get("reply", body))
        except ValueError:
            on_token(body)
        return
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    while True:
        raw = r.read(256)
        if not raw:
            break
        tok = dec.decode(raw)
        if tok:
            on_token(tok)
    tail = dec.decode(b"", True)
    if tail:
        on_token(tail)


# ---- Routing table ---------------------------------------------------------
# Erster Schritt weg von der if-Kette (Strangler wie beim mgr/-Paket): wer hier
# steht, wird ueber die Tabelle zugestellt; alles andere faellt weiter durch die
# Kette. Eine Route liefert (body, content_type) und ueberlaesst das Senden dem
# Verteiler — oder None, wenn sie selbst geantwortet hat.
from mgr import saddler as _saddler_mod  # noqa: E402
_saddler_mod.configure(_audit.AUDIT_DIR, _store.HISTORY_DB)

from mgr import websearch as _websearch_mod  # noqa: E402
_websearch_mod.configure(lambda key: (_settings.load_settings().get(key) or ""))


def _ha_ws_target():
    """(host, port) des Home-Assistant-WebSocket aus dem MCP-Katalog. Der
    'homeassistant'-Eintrag traegt die URL in args[0]; wir leiten daraus die
    WS-Adresse ab (kein zusaetzlicher Config-Ort)."""
    for m in _mcp.load_mcps():
        if m.get("name") == "homeassistant":
            for a in m.get("args", []):
                a = str(a)
                if a.startswith(("http://", "https://")):
                    hostport = a.split("//", 1)[1].split("/", 1)[0]
                    host, _, port = hostport.partition(":")
                    return host, int(port or "8123")
    return None


from mgr import haalias as _haalias  # noqa: E402
_haalias.configure(_ha_ws_target, lambda: _secrets.secret_store().get("HA_TOKEN"))

ROUTER = _routes.Router()


@ROUTER.get("/api/instances", admin=True)
def _rt_instances(h):
    return (json.dumps([{**i, "running": _instances.is_running(i), "stale": image_state(i)[0]}
                        for i in _instances.load_instances()]).encode(),
            "application/json")


TEMPLATE_RUNTIME = {"openrouter": "openrouter-agent", "orcarouter": "openrouter-agent",
                    "llama": "openrouter-agent (local model)", "claude": "claude-code",
                    "pi": "pi", "prime": "prime"}


def claude_login_state():
    """The host's Claude login as the session panel shows it: not only present,
    but how long its access token is still valid (the VM works from a copy)."""
    try:
        with open(_secrets.CLAUDE_CRED_SRC) as fh:
            exp = (json.load(fh).get("claudeAiOauth") or {}).get("expiresAt") or 0
    except (OSError, ValueError):
        return "missing (log in on the host)"
    left = exp / 1000 - time.time()
    if left <= 0:
        return "expired on the host (run claude /login there)"
    return f"ok · valid {int(left // 3600)}h {int(left % 3600 // 60)}m"


def session_info(inst):
    """What the chat's session panel shows for an instance: runtime, uptime,
    login state, the platform services as the agent sees them, and its MCP
    servers with whether their secrets are released. Nothing secret in it."""
    name, tpl = inst["name"], inst.get("template", "")
    cfg = inst.get("config") or {}
    running = _instances.is_running(inst)
    try:
        started = os.path.getmtime(_instances.pidfile(inst)) if running else 0
    except OSError:
        started = 0
    if tpl == "claude":
        login = claude_login_state()
    elif (_settings.load_settings().get("LLM_KEY_PROXY") or "") == "1":
        login = "key proxy"
    else:
        keyname = "ORCAROUTER_API_KEY" if tpl in ("orcarouter", "llama") else "OPENROUTER_API_KEY"
        login = "api key" if _secrets.secret_store().get(keyname) else "no key"
    mem_dir = _memfs.folder(name) if uses_harness(inst) else None
    notes = 0
    if mem_dir:
        try:
            notes = len([f for f in os.listdir(os.path.join(mem_dir, "notes")) if f.endswith(".md")])
        except OSError:
            pass
    try:
        with _store._hist_lock, _store._hist_conn() as c:
            sem = c.execute("SELECT COUNT(*) FROM semantic_memory WHERE instance=?", (name,)).fetchone()[0]
    except Exception:
        sem = 0
    platform = [
        {"name": "Memory", "state": (f"{notes} notes · {sem} semantic" if (notes or sem) else "empty")
                            + (" · hindsight" if _hindsight.enabled() else ""), "ok": True},
        {"name": "Web search", "state": "reachable" if (_settings.load_settings().get("BRAVE_API_KEY") or "") else "DuckDuckGo fallback", "ok": True},
        {"name": "Skills", "state": f"{len(load_skills())} in catalog", "ok": True},
        {"name": "Traces", "state": f"{len(_store.turns_read(name, limit=50))} recent turns", "ok": True},
    ]
    allowed = _secrets.allowed_secret_keys(inst)
    mcps = []
    for n in [x for x in (cfg.get("MCP_SERVERS", "") or "").split(",") if x]:
        missing = sorted(_mcp.mcp_required_secrets([n]) - allowed)
        mcps.append({"name": n, "ready": not missing, "missing": missing})
    return {"name": name, "template": tpl, "runtime": TEMPLATE_RUNTIME.get(tpl, tpl or "agent"),
            "running": running, "uptime": int(time.time() - started) if started else 0,
            "model": cfg.get("OPENROUTER_MODEL") or cfg.get("ANTHROPIC_MODEL") or "",
            "stale": image_state(inst)[0], "login": login,
            "platform": platform, "mcps": mcps,
            "need_secret": sum(1 for m in mcps if not m["ready"])}


@ROUTER.get("/api/session/", prefix=True, admin=True)
def _rt_session(h):
    # /api/session/<instance>        -> the session panel's data
    # /api/session/<instance>/log    -> the VM's console log tail (text)
    parts = _tail(h, "/api/session/")
    nm = re.sub(r"[^a-zA-Z0-9_-]", "", parts[0] if parts else "")
    inst = next((i for i in _instances.load_instances() if i["name"] == nm), None)
    if inst is None:
        return h._json({"error": "unknown instance"}, 404)
    if len(parts) > 1 and parts[1] == "log":
        try:
            with open(os.path.join(_paths.RUN_DIR, f"{nm}.log"), "rb") as fh:
                fh.seek(0, 2); size = fh.tell(); fh.seek(max(0, size - 64 * 1024))
                data = fh.read()
        except OSError:
            data = b"(no log yet)"
        return data, "text/plain; charset=utf-8"
    return h._json(session_info(inst))


@ROUTER.get("/api/settings", admin=True)
def _rt_settings(h):
    return json.dumps(_settings.settings_for_ui()).encode(), "application/json"


# What TTS should NOT read: tool status lines ("🔧 ha_control …"), think blocks,
# code fences, markdown decor, URLs. The desktop client filters this itself;
# the web chat's "Read aloud", the app and the ESP client send the reply as
# is — so the manager filters once for everyone, right before Piper.
@ROUTER.get("/api/stt-recent", admin=True)
def _rt_stt_recent(h):
    return json.dumps({"recent": _voice.stt_recent()}, ensure_ascii=False).encode(), "application/json"


@ROUTER.get("/api/stt-recent/audio", admin=True)
def _rt_stt_audio(h):
    q = urllib.parse.parse_qs(h.path.partition("?")[2])
    try:
        i = int(q.get("i", ["0"])[0])
    except ValueError:
        i = 0
    item = _voice.stt_audio(i)
    if not item:
        return json.dumps({"error": "no audio kept"}).encode(), "application/json"
    return item[3], item[2] or "application/octet-stream"


@ROUTER.get("/api/tasks", admin=True)
def _rt_tasks(h):
    return json.dumps(_store.load_tasks()).encode(), "application/json"


@ROUTER.get("/api/usage", admin=True)
def _rt_usage(h):
    return json.dumps(_store.usage_summary()).encode(), "application/json"


@ROUTER.get("/api/usage-by-model", admin=True)
def _rt_usage_by_model(h):
    try:
        since = int(_qs(h).get("since", ["0"])[0] or 0)
    except ValueError:
        since = 0
    return h._json({"rows": _store.usage_by_model(since)})


@ROUTER.get("/api/version", admin=True)
def _rt_version(h):
    u = _about.update_check(force="force" in _qs(h))
    inst = _about.installed_version()
    return h._json({"installed": inst, "latest": u["latest"], "url": u["url"], "notes": u["notes"],
                    "error": u["error"], "available": _about.update_available(inst, u["latest"]), **_about.update_status()})


@ROUTER.get("/api/gateway", admin=True)
def _rt_gateway(h):
    g = _gateway.load_gateway()
    g["available"] = _gateway._clean_unicode is not None
    return json.dumps(g).encode(), "application/json"


@ROUTER.get("/api/personas")
def _rt_personas(h):
    return json.dumps(load_personas(), ensure_ascii=False).encode(), "application/json"


@ROUTER.get("/api/skills")
def _rt_skills(h):
    # ?meta=1: name + description only. The full catalog is ~870 KB with the
    # bodies — the agents call this on every list_skills and never need them.
    q = urllib.parse.parse_qs(h.path.partition("?")[2])
    items = load_skills()
    if q.get("meta", ["0"])[0] == "1":
        items = [{"name": x.get("name", ""), "description": x.get("description", "")}
                 for x in items]
    return json.dumps(items, ensure_ascii=False).encode(), "application/json"


@ROUTER.get("/api/skills/", prefix=True)
def _rt_skill(h):
    nm = re.sub(r"[^a-z0-9_-]", "", h.path.split("/api/skills/", 1)[1].lower())
    sk = next((x for x in load_skills() if x.get("name") == nm), None)
    return ((sk.get("content", "") if sk else f"Skill '{nm}' not found").encode(),
            "text/plain; charset=utf-8")


@ROUTER.get("/api/saddler")
def _rt_saddler(h):
    # Weekly failure digest over ALL instances' audits. That is cross-instance
    # information, so guests may not read it — except the orchestrator, whose
    # scheduled saddler task is the intended consumer.
    g = instance_by_ip(h.client_address[0])
    if g is not None and g.get("name") != ORCH_INSTANCE:
        return json.dumps({"error": "orchestrator only"}).encode(), "application/json"
    q = urllib.parse.parse_qs(h.path.partition("?")[2])
    try:
        days = max(1, min(int(q.get("days", ["7"])[0]), 60))
    except ValueError:
        days = 7
    d = _saddler_mod.digest(days)
    d["text"] = _saddler_mod.render(d)
    return json.dumps(d, ensure_ascii=False).encode(), "application/json"


@ROUTER.get("/api/websearch")
def _rt_websearch(h):
    # Web search for the agents: the Brave key stays on the host, the VM only
    # ever sees results. Same principle as the LLM key proxy. Metered per
    # guest: the key's quota is shared by every instance.
    from mgr import websearch
    g = h._guest()
    if g is not None and not tool_allowed(g, "web_search"):
        return h._json({"error": "web_search not allowed for this instance"}, 403)
    if g is not None and not _util.rate_ok(("websearch", g["name"]), 30, 300):
        return h._json({"error": "rate limit: 30 searches per 5 minutes"}, 429)
    q = urllib.parse.parse_qs(h.path.partition("?")[2])
    query = q.get("q", [""])[0].strip()
    if not query:
        return json.dumps({"error": "q missing"}).encode(), "application/json"
    count = q.get("count", ["5"])[0]
    out = {"result": websearch.web_search(query, count)}
    return json.dumps(out, ensure_ascii=False).encode(), "application/json"


@ROUTER.post("/api/extract", admin=True)
def _rt_extract(h):
    # Chat attachment: PDF/DOCX/text in, extracted text out. The app puts the
    # text into the message; the model never sees the binary. Admin-only: this
    # is a client feature, agents extract inside their VM (read_pdf).
    from mgr.extract import extract_document
    name = urllib.parse.parse_qs(h.path.partition("?")[2]).get("name", ["upload"])[0]
    ln = int(h.headers.get("Content-Length", 0) or 0)
    if ln > 50 * 1024 * 1024:
        return json.dumps({"error": "file larger than 50 MB"}).encode(), "application/json"
    data = h.rfile.read(ln)
    try:
        text, note = extract_document(name, data)
        out = {"name": name, "text": text, "chars": len(text)}
        if note:
            out["note"] = note
    except ValueError as e:
        out = {"error": str(e)}
    except Exception as e:
        out = {"error": f"extraction failed: {e!r}"}
    return json.dumps(out, ensure_ascii=False).encode(), "application/json"


@ROUTER.get("/api/prompts")
def _rt_prompts(h):
    return (json.dumps({"prompts": _rules.load_prompts()}, ensure_ascii=False).encode(),
            "application/json")


@ROUTER.get("/api/resources", admin=True)
def _rt_resources(h):
    return json.dumps({"resources": resource_stats()}).encode(), "application/json"


@ROUTER.get("/api/iroh")
def _rt_iroh_status(h):
    return json.dumps(_irohgw.status()).encode(), "application/json"


@ROUTER.get("/api/voice-health")
def _rt_voice_health(h):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{_settings.VOICE_PORT}/health", timeout=5) as r:
            out = r.read()
    except Exception as e:
        out = json.dumps({"ready": False, "error": str(e)}).encode()
    return out, "application/json"


@ROUTER.get("/logo.svg")
@ROUTER.get("/favicon.ico")
def _rt_logo(h):
    return LOGO_SVG.encode(), "image/svg+xml"


class H(BaseHTTPRequestHandler):
    # Reading a request (headers, body) may not hang a thread for ever; the
    # tunnel and the long-polls lift this per socket / wait server-side.
    timeout = 120
    # Protection layer: an unhandled exception in a route must NOT tear the
    # connection down hard (the agent would otherwise see "RemoteDisconnected").
    # If no header has been sent yet, we respond cleanly with HTTP 500; otherwise
    # the response is just ended. The error lands in the journal.
    def end_headers(self):
        self._sent = True
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        return super().end_headers()

    def _fail500(self):
        import traceback
        tb = traceback.format_exc()
        print(f"[http] unhandled in {self.command} {self.path}:\n{tb}", flush=True)
        if getattr(self, "_sent", False):
            return
        try:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"internal server error"}')
        except Exception:
            pass

    def do_GET(self):
        self._sent = False
        try:
            self._do_GET()
        except Exception:
            self._fail500()

    def do_POST(self):
        self._sent = False
        try:
            self._do_POST()
        except _util.BodyTooLarge as e:
            self.close_connection = True          # the body was never read
            if not getattr(self, "_sent", False):
                self._json({"error": f"body too large ({e} bytes)"}, 413)
        except Exception:
            self._fail500()

    def _forbid(self):
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error":"forbidden"}')

    def _auth(self):
        # Guests (VMs) carry no credentials: they are identified by source IP
        # and gated by the guest allow/deny lists. Without this exemption a set
        # MANAGER_PASS would lock every agent out of its own manager.
        if not _auth.PW or instance_by_ip(self.client_address[0]) is not None:
            return True
        # Host services (containers on the docker bridge, e.g. Hindsight) may
        # use the key proxy without a login — only that path, only from there.
        if self.path.startswith("/api/llm/") and self.client_address[0].startswith("172.17."):
            return True
        key = _auth.auth_client_key(self.client_address[0], self.headers.get("X-Forwarded-For", ""))
        if _auth.auth_locked(key):
            self._send(b'{"error":"too many failed logins, try again later"}', "application/json", 429)
            return False
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                u, p = base64.b64decode(hdr[6:]).decode().split(":", 1)
                if hmac.compare_digest(u.encode(), str(_auth.USER).encode()) and \
                        hmac.compare_digest(p.encode(), str(_auth.PW).encode()):
                    _auth.auth_succeeded(key)
                    return True
            except Exception:
                pass
            if _auth.auth_failed(key):
                print(f"[auth] {key}: {_auth.AUTH_FAILS_MAX} failed logins, locked for {_auth.AUTH_LOCK // 60} min", flush=True)
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="kAIm56"')
        self.end_headers()
        return False

    def log_message(self, *a):
        pass

    def _chat_stream(self, name):
        """POST /api/chat/<instance> -> response tokens as raw text (stream)."""
        try:
            ln = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(ln) or b"{}")
        except (ValueError, json.JSONDecodeError):
            body = {}
        inst = next((i for i in _instances.load_instances() if i["name"] == name
                     and (i.get("config") or {}).get("TRANSPORT") == "web"), None)
        self.send_response(200 if inst else 404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        parts = []      # what the caller actually received (post-gateway)

        def emit(tok):
            parts.append(tok)
            try:
                self.wfile.write(tok.encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass

        if not inst:
            return emit(f"⚠️ No web instance '{name}'.")
        if not wait_web(inst):
            return emit(f"⚠️ Instance '{name}' does not start (port {_instances.WEB_GUEST_PORT}).")

        chat_id = body.get("chat")
        msg, img = body.get("message", ""), body.get("image")
        if _gateway.gateway_on(chat_id):
            msg = _gateway.gateway_clean(msg, chat_id, "in")
            if img:
                img, k = _gateway.strip_image_meta(img)
                _gateway.gateway_count(chat_id, "img", k)
            guard = _gateway.StreamGuard(chat_id)
            raw_emit, emit = emit, lambda t: raw_emit(guard.feed(t))
        else:
            guard = None
        try:
            guest_stream(inst, msg, img, emit)
        except Exception as e:
            emit(f"\n⚠️ {e!r}")
        finally:
            if guard:
                raw_emit(guard.flush())
        # Mirror voice turns into the shared chat store (see voice_session).
        try:
            text = str(msg or "").strip()
            src = self.client_address[0]
            if text == "/reset":
                voice_session(name, src, chat_id or "", reset=True)
            elif text and not text.startswith("/"):
                sid = voice_session(name, src, chat_id or "")
                if sid:
                    chat_log_append(name, sid, text, "".join(parts).strip(), kind="voice")
        except Exception as e:
            print(f"[quiet] voice chat mirror failed: {e!r}", flush=True)

    def _term_route(self, name, tail):
        """Route /i/<name>/term[/...] to the guest webterm (:7682). WS-aware."""
        inst = next((i for i in _instances.load_instances() if i["name"] == name), None)
        if not inst or not _instances.is_running(inst):
            self.send_response(503)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<p>Instance '{name}' is not running (terminal unavailable).</p>".encode())
            return
        sub = tail[len("term"):].lstrip("/")  # "" | "ws"
        if "upgrade" in self.headers.get("Connection", "").lower() and \
           self.headers.get("Upgrade", "").lower() == "websocket":
            # H-2 (security review): a WebSocket handshake is a GET, the browser
            # attaches the cached Basic-auth and same-origin policy does not
            # apply — so any page the admin visits could open a shell in a VM.
            # Browsers ALWAYS send Origin on a WS handshake: require it and
            # require it to be ours (the empty-Origin allowance of origin_allowed
            # is for curl/app POSTs, not for this path).
            origin = (self.headers.get("Origin") or "").strip()
            if not origin or not _auth.origin_allowed(origin):
                self.send_response(403)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"terminal websocket: origin not allowed")
                return
            return self._ws_tunnel(_instances.net_of(inst)["guest"], _instances.TERM_GUEST_PORT, "/" + sub)
        if sub == "":
            # H-3 (security review): the terminal PAGE is served by the manager
            # from its own copy of webterm.py, never fetched from the guest —
            # an agent that replaced the page on :7682 (same uid as webterm)
            # could otherwise run script on the manager origin with the
            # admin's session. Only the /ws frames are tunneled to the VM.
            try:
                from webterm import PAGE as _term_page
            except Exception:
                _term_page = None
            if _term_page:
                body = _term_page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        return self._proxy("GET", port=_instances.TERM_GUEST_PORT, tail_override=sub)

    def _ws_tunnel(self, guest, port, path):
        """Raw bidirectional splice of a WebSocket between browser and guest."""
        try:
            up = socket.create_connection((guest, port), timeout=10)
        except OSError as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f"terminal connect failed: {e!r}".encode())
            return
        # Otherwise the connect timeout stays as a READ timeout on the socket —
        # after 10 s of idling recv() tore the tunnel down ("connection closed").
        # A terminal may be silent arbitrarily long: timeouts off, but TCP
        # keepalive instead, so half-dead connections still die.
        up.settimeout(None)
        down_sock = self.connection
        try:
            down_sock.settimeout(None)
            for sk in (up, down_sock):
                sk.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        # Replay the client's upgrade request to the guest webterm — only the
        # headers the upgrade needs: the browser's Authorization (Traefik's
        # BasicAuth passes it on) and cookies must never land in a VM.
        req = f"GET {path} HTTP/1.1\r\n"
        for k, v in ws_forward_headers(self.headers.items()):
            req += f"{k}: {v}\r\n"
        req += "\r\n"
        up.sendall(req.encode())
        self.close_connection = True
        down = self.connection

        def pipe(a, b):
            try:
                while True:
                    data = a.recv(65536)
                    if not data:
                        break
                    b.sendall(data)
            except OSError:
                pass
            finally:
                for s in (a, b):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        t = threading.Thread(target=pipe, args=(up, down), daemon=True)
        t.start()
        pipe(down, up)   # blocks until browser->guest side ends
        t.join(timeout=1)
        for s in (up, down):
            try:
                s.close()
            except OSError:
                pass

    def _katfs_proxy(self):
        """Pass through the katfs node's share page under /katfs/. GET only —
        the page loads assets relatively and then speaks P2P (WASM/iroh), it
        needs nothing else from the node. Purpose: same origin as the manager,
        i.e. HTTPS behind Traefik → the File System Access API works."""
        rest, _, qs = (self.path[len("/katfs"):] or "/").partition("?")
        # ?key=… replaces the node-id inserted by the node in the #nodeid field —
        # so this browser can also deliver a folder to a *foreign* katfs node.
        # Strictly filtered, the value lands in an attribute.
        key = re.sub(r"[^A-Za-z0-9._-]", "",
                     urllib.parse.parse_qs(qs).get("key", [""])[0])[:200]
        try:
            with urllib.request.urlopen(_katfs.KATFS_BASE + rest, timeout=10) as r:
                body = r.read()
                ct = r.headers.get("Content-Type", "application/octet-stream")
            if key and ct.startswith("text/html"):
                body = re.sub(rb'(<input id="nodeid"[^>]*value=")[^"]*(")',
                              lambda m: m.group(1) + key.encode() + m.group(2),
                              body, count=1)
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<h3>katfs node not reachable</h3>"
                             f"<p>{_katfs.KATFS_BASE} — {e}</p>".encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, method, port=_instances.WEB_GUEST_PORT, tail_override=None):
        rest = self.path[3:]  # strip "/i/"
        name, _, tail = rest.partition("/")
        if tail_override is not None:
            tail = tail_override
        inst = next((i for i in _instances.load_instances() if i["name"] == name), None)
        if not inst or not _instances.is_running(inst):
            # The app puts the body of an API answer straight into the chat bubble —
            # HTML would show up there as raw <p>…</p>. So: markup only for the
            # browser paths, plain text for /api/….
            api = tail.split("?", 1)[0].startswith("api/")
            msg = f"Instance '{name}' is not running (web UI unavailable)."
            self.send_response(503)
            self.send_header("Content-Type",
                             "text/plain; charset=utf-8" if api else "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write((msg if api else f"<p>{msg}</p>").encode())
            return
        url = f"http://{_instances.net_of(inst)['guest']}:{port}/{tail}"
        data = None
        if method == "POST":
            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))

        # The app doesn't chat via /api/chat/<inst> but through here — so the
        # gateway has to sit at both entrances, not just the more convenient one.
        guard = None
        if method == "POST" and tail.split("?", 1)[0] in ("api/chat", "api/chat/stream"):
            try:
                b = json.loads(data or b"{}")
            except (ValueError, TypeError):
                b = None
            if isinstance(b, dict):
                chat_id = b.pop("chat", None)      # the guest doesn't know it, stays here
                if _gateway.gateway_on(chat_id):
                    b["message"] = _gateway.gateway_clean(b.get("message", ""), chat_id, "in")
                    if b.get("image"):
                        b["image"], k = _gateway.strip_image_meta(b["image"])
                        _gateway.gateway_count(chat_id, "img", k)
                    guard = _gateway.StreamGuard(chat_id)
                if chat_id is not None:
                    data = json.dumps(b).encode()

        req = urllib.request.Request(url, data=data, method=method)
        if self.headers.get("Content-Type"):
            req.add_header("Content-Type", self.headers["Content-Type"])
        try:
            r = urllib.request.urlopen(req, timeout=620)
            # Bridges without streaming (the claude template) answer with
            # {"reply": …} and Content-Type application/json — even on
            # /api/chat/stream. But the app reads the body as raw text and would
            # otherwise show the bare JSON including \uXXXX. So unpack it here and
            # forward it as text/plain, as guest_stream has long done for the web
            # chat. The Content-Type is fixed BEFORE sending.
            chat_path = tail.split("?", 1)[0] in ("api/chat", "api/chat/stream")
            is_json = "json" in (r.headers.get("Content-Type") or "").lower()
            if chat_path and is_json:
                body = r.read()
                try:
                    reply = json.loads(body).get("reply", body.decode("utf-8", "replace"))
                except (ValueError, AttributeError):
                    reply = body.decode("utf-8", "replace")
                if guard is not None:
                    reply = _gateway.gateway_clean(reply, guard.chat_id, "out")
                out = reply.encode("utf-8")
                self.send_response(r.status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(out)))
                if r.headers.get("X-Kaim-Turn"):
                    self.send_header("X-Kaim-Turn", r.headers["X-Kaim-Turn"])
                self.end_headers()
                self.wfile.write(out)
                return
            self.send_response(r.status)
            ct = r.headers.get("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Type", ct)
            if "html" in ct.lower():
                # H-3: guest-authored HTML must not run on the manager's origin
                # with the admin's session. `sandbox` gives it an opaque origin:
                # no script, no credentialed access to /api/*. (The terminal page
                # is served by the manager itself, see _term_route.)
                self.send_header("Content-Security-Policy", "sandbox")
            if r.headers.get("X-Kaim-Turn"):        # the turn id: the app fetches its trace by it
                self.send_header("X-Kaim-Turn", r.headers["X-Kaim-Turn"])
            self.end_headers()
            # Pass through chunk by chunk + flush -> token streaming from the
            # agent. With the gateway a decoder runs in between: otherwise 4-KB
            # cuts would fall in the middle of a multi-byte character.
            dec = codecs.getincrementaldecoder("utf-8")() if guard is not None else None
            while True:
                chunk = r.read(4096)
                if not chunk:
                    break
                if guard is not None:
                    chunk = guard.feed(dec.decode(chunk)).encode("utf-8")
                    if not chunk:
                        continue
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except Exception:
                    break
            if guard is not None:
                # Flush the decoder first, then the buffer — the other way round
                # the last character would come through unfiltered.
                rest = (guard.feed(dec.decode(b"", True)) + guard.flush()).encode("utf-8")
                if rest:
                    try:
                        self.wfile.write(rest)
                        self.wfile.flush()
                    except Exception:
                        pass
            return
        except urllib.error.HTTPError as e:
            body, status, rct = e.read(), e.code, e.headers.get("Content-Type", "text/plain")
        except Exception as e:
            body, status, rct = f"proxy error: {e!r}".encode(), 502, "text/plain"
        self.send_response(status)
        self.send_header("Content-Type", rct)
        self.end_headers()
        self.wfile.write(body)

    def _do_GET(self):
        if not self._auth():
            return
        if self._dispatch("GET"):
            return
        # No route: the admin UI for everything else (index, deep links).
        # Guests get nothing here; unknown API paths a clean 404.
        if instance_by_ip(self.client_address[0]) is not None:
            return self._forbid()
        if self.path.split("?", 1)[0].startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        body = render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        # Never cache: a stale manager page after an update produces ghost
        # errors (old JS logic against a new API).
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _llm_proxy(self, _pp):
        """POST /api/llm/<backend>/chat/completions — credential injection
        gateway. The body goes unchanged to the router; the manager injects the
        Authorization header from the settings so the key never reaches the VM.
        Streams (SSE) are passed through line by line, upstream errors
        transparently (status + body). Deliberately NO logs of key or body —
        those are exactly what should not leave the host or linger anywhere."""
        parts = _pp.strip("/").split("/")      # api/llm/<backend>/chat/completions
        backend = parts[2] if len(parts) > 2 else ""
        if backend not in _llmproxy.LLM_PROXY_UPSTREAMS or parts[3:] != ["chat", "completions"]:
            out = b'{"error":"unknown llm proxy path"}'
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers(); self.wfile.write(out); return
        ok_g, why = _llmproxy._guard_check(instance_by_ip(self.client_address[0]))
        if not ok_g:
            out = json.dumps({"error": {"message": f"guardrail: {why}", "code": 429}}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers(); self.wfile.write(out); return
        url, keyname = _llmproxy.LLM_PROXY_UPSTREAMS[backend]
        st = _settings.load_settings()
        # Self-hosted OrcaRouter-Lite: the shared base URL applies to the proxy
        # too — otherwise the detour would suddenly run against the cloud while
        # direct mode talks to the own server.
        if backend == "orcarouter" and (st.get("ORCAROUTER_URL") or "").strip():
            u = st["ORCAROUTER_URL"].strip().rstrip("/")
            if not u.endswith("/chat/completions"):
                u += "/chat/completions" if u.endswith("/v1") else "/v1/chat/completions"
            url = u
        key = (st.get(keyname) or "").strip()
        if not key:
            out = json.dumps({"error": f"{keyname} not configured on host"}).encode()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers(); self.wfile.write(out); return
        payload = self._raw(BODY_MAX_LLM)
        ginst = instance_by_ip(self.client_address[0])
        if ginst is None and self.client_address[0].startswith("172.17."):
            ginst = {"name": "hindsight" if _hindsight.enabled() else "services"}   # booked, not a VM
        span = {"turn": self.headers.get("X-Kaim-Turn", "")[:16],
                "step": self.headers.get("X-Kaim-Step", "") or None}
        _t0 = time.monotonic()
        try:
            want_stream = bool(json.loads(payload or b"{}").get("stream"))
        except (ValueError, AttributeError):
            want_stream = False
        req = urllib.request.Request(url, data=payload, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "HTTP-Referer": f"https://{_settings.PUBLIC_HOST}",
            "X-Title": "kat56-agent"})
        try:
            r = urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as e:
            # Pass upstream errors through 1:1: the agent has its own retry
            # logic for 429/5xx and shows 4xx bodies as an error message.
            data = e.read()
            if ginst is not None:            # a failed LLM span, with the reason
                _store.usage_add(ginst["name"], backend, 0, 0, 0, ok=False,
                          err=f"HTTP {e.code}: {data[:300].decode('utf-8', 'replace')}",
                          ms=int((time.monotonic() - _t0) * 1000), **span)
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data); return
        except Exception as e:
            data = json.dumps({"error": f"llm upstream unreachable: {e!r}"}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data); return
        with r:
            self.send_response(r.status)
            self.send_header("Content-Type", r.headers.get("Content-Type", "application/json"))
            if want_stream:
                # Write SSE on line by line and flush — full buffering would kill
                # the token streaming in the agent. readline() blocks only until
                # the next event line, never until the end of the stream. Without
                # Content-Length the response ends with the connection close
                # (HTTP/1.0), urllib in the guest reads until EOF.
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                try:
                    while True:
                        chunk = r.readline()
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        if b'"usage"' in chunk:
                            _llmproxy._proxy_usage(ginst, backend, chunk, ms=int((time.monotonic() - _t0) * 1000), **span)
                except (BrokenPipeError, ConnectionResetError):
                    pass               # client gone -> upstream closes via with
            else:
                data = r.read()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                _llmproxy._proxy_usage(ginst, backend, data, ms=int((time.monotonic() - _t0) * 1000), **span)

    def _do_POST(self):
        if not self._auth():
            return
        p = self.path.split("?", 1)[0]
        guest = instance_by_ip(self.client_address[0])
        if guest is None and not _auth.origin_allowed(self.headers.get("Origin", "")):
            return self._json({"error": "cross-site request refused"}, 403)
        if guest is not None and not (
                p in GUEST_POST_PATHS or p.startswith(GUEST_POST_PREFIXES)):
            return self._forbid()
        if self._dispatch("POST"):
            return
        self._json({"msg": "unknown"}, 404)

    def _dispatch(self, method):
        """Route-table lookup. True when a route answered (or was forbidden)."""
        hit = ROUTER.resolve(method, self.path)
        if hit is None:
            return False
        fn, admin_only = hit
        if admin_only and instance_by_ip(self.client_address[0]) is not None:
            self._forbid()
            return True
        out = fn(self)
        if out is not None:
            body, ct = out
            self._send(body, ct)
        return True

    # ---- small helpers every route uses ------------------------------------
    def _send(self, body, ct="application/json", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode(), "application/json", code)

    def _raw(self, limit=None):
        """Request body, capped (BODY_MAX unless the route says otherwise);
        a missing or bad length is an empty body. Over the cap: BodyTooLarge,
        answered 413 by do_POST without reading a byte of it."""
        try:
            ln = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            ln = 0
        if ln > (limit or BODY_MAX):
            raise _util.BodyTooLarge(ln)
        return self.rfile.read(ln) if ln > 0 else b""

    def _body(self, default=None):
        """JSON body; {} (or `default`) when empty or malformed."""
        raw = self._raw()
        if not raw:
            return {} if default is None else default
        try:
            return json.loads(raw)
        except ValueError:
            return {} if default is None else default

    def _guest(self):
        return instance_by_ip(self.client_address[0])


# =============================================================================
# HTTP routes — one function per path, registered in ROUTER. The handler
# methods _do_GET/_do_POST only authenticate, apply the guest allow/deny
# lists, dispatch through the table and fall back to the admin UI / 404.
# A route gets the handler `h`; it either returns (body, content-type) for a
# plain 200 or answers itself via h._json(obj, code) / h.wfile and returns
# None. `admin=True` = guests (VMs, identified by source IP) get 403.
# Grouped by domain; new routes go HERE, never into an if-chain.
# =============================================================================

_WS_KEEP = ("host", "upgrade", "connection", "origin", "user-agent", "pragma", "cache-control")


def ws_forward_headers(items):
    """The subset of a browser's upgrade headers the guest terminal gets."""
    return [(k, v) for k, v in items
            if k.lower() in _WS_KEEP or k.lower().startswith("sec-websocket-")]


def _msg_route(method, path, prefix=False, admin=True):
    """Decorator for the {"msg": …} family (UI actions): the function returns
    a status string; exceptions become "error: …" instead of a dropped
    connection, exactly as the old chain did."""
    def deco(fn):
        def wrapped(h):
            try:
                msg = fn(h)
            except _util.BodyTooLarge:
                raise
            except Exception as e:
                msg = f"error: {e!r}"
            return json.dumps({"msg": msg}).encode(), "application/json"
        wrapped.__name__ = fn.__name__
        ROUTER.add(method, path, wrapped, prefix=prefix, admin=admin)
        return fn
    return deco


def _qs(h):
    return urllib.parse.parse_qs(h.path.partition("?")[2])


def _tail(h, prefix):
    """Path remainder after `prefix`, query stripped, URL-decoded per segment."""
    return [urllib.parse.unquote(x) for x in h.path.split("?", 1)[0][len(prefix):].split("/")]


# ---- pages and proxies ------------------------------------------------------
@ROUTER.get("/chat", admin=True)
def _rt_chat_page(h):
    want = _qs(h).get("i", [""])[0]
    body = chatui.render(web_instances(), want, LOGO_INLINE).encode()
    h.send_response(200)
    h.send_header("Content-Type", "text/html; charset=utf-8")
    # Don't cache: otherwise the browser holds on to an old version (that
    # was the cause of the gray emoji boxes after the icon fix).
    h.send_header("Cache-Control", "no-store, must-revalidate")
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


@ROUTER.get("/katfs", admin=True)
def _rt_katfs_redirect(h):
    h.send_response(301)
    h.send_header("Location", "/katfs/")
    h.end_headers()


@ROUTER.get("/katfs/", prefix=True, admin=True)
def _rt_katfs_proxy(h):
    return h._katfs_proxy()


@ROUTER.get("/i/", prefix=True, admin=True)
def _rt_instance_proxy_get(h):
    name, _, tail = h.path[3:].partition("/")
    if tail.split("?", 1)[0].rstrip("/").split("/")[0] == "term":
        return h._term_route(name, tail.split("?", 1)[0])
    return h._proxy("GET")


@ROUTER.post("/i/", prefix=True, admin=True)
def _rt_instance_proxy_post(h):
    return h._proxy("POST")


@ROUTER.post("/api/chat/", prefix=True, admin=True)
def _rt_chat_stream(h):
    return h._chat_stream(urllib.parse.unquote(h.path[len("/api/chat/"):].split("?", 1)[0]))


@ROUTER.post("/api/llm/", prefix=True)
def _rt_llm_proxy(h):
    # LLM key injection: streamed, so it answers itself.
    return h._llm_proxy(h.path.split("?", 1)[0])


# ---- secrets, credentials, MCP config (guests, by source IP) ----------------
@ROUTER.get("/api/secrets")
def _rt_secrets(h):
    # What get_secret may fetch: released AND guest-readable.
    inst = h._guest()
    keys = sorted(_secrets.guest_readable_keys(inst)) if inst else []
    return h._json({"allowed": keys, "instance": inst.get("name") if inst else None})


@ROUTER.get("/api/claude-credentials")
def _rt_claude_credentials(h):
    # Subscription login for the claude template: the guest fetches the LIVE
    # credential of the host at boot. Only the claudeAiOauth block — the
    # mcpOAuth tokens are none of the VM's business. Strictly gated: only a
    # real guest whose instance runs the claude template.
    inst = h._guest()
    if inst is None or inst.get("template") != "claude":
        return h._json({"error": "claude template guests only"}, 403)
    try:
        with open(_secrets.CLAUDE_CRED_SRC) as fh:
            full = json.load(fh)
        return h._json({"claudeAiOauth": full["claudeAiOauth"]})
    except (OSError, ValueError, KeyError):
        return h._json({"error": "no host credential (run claude /login on the host)"}, 503)


@ROUTER.get("/api/secret/", prefix=True)
def _rt_secret(h):
    name = h.path.split("/api/secret/", 1)[1]
    inst = h._guest()
    if inst is None or name not in _secrets.guest_readable_keys(inst):
        return h._json({"error": "not allowed (released for the hub only, or not released)"}, 403)
    return h._json({"value": _secrets.secret_store().get(name, "")})


@ROUTER.get("/api/mcp-config")
def _rt_mcp_config(h):
    # Counterpart to /api/secret/<name>, but for MCP: only the guest itself,
    # only its own servers. Since the hub the processes run on the host — the
    # guest needs the NAMES; secrets stay ${PLACEHOLDER} and never leave.
    inst = h._guest()
    if inst is None:
        return h._json({"error": "guests only"}, 403)
    names = [n for n in (inst.get("config", {}).get("MCP_SERVERS", "") or "").split(",") if n]
    blob = _mcp.build_mcp_config(names, allowed=set()) if names else ""
    missing = sorted(_mcp.mcp_required_secrets(names) - _secrets.allowed_secret_keys(inst))
    data = json.loads(blob) if blob else {"mcpServers": {}}
    if missing:
        data["unresolved"] = missing
    return h._json(data)


@ROUTER.post("/api/mcp")
def _rt_mcp_call(h):
    # A guest's MCP call -> hub. The instance comes from the source IP; the
    # admin can pass "instance" in the body for testing.
    b = h._body()
    inst = h._guest()
    if inst is None and b.get("instance"):
        inst = next((i for i in _instances.load_instances() if i["name"] == b["instance"]), None)
    if inst is None:
        return h._json({"error": "unknown caller"}, 403)
    st, out = _mcp.mcp_hub_call(inst, str(b.get("server") or ""), b.get("payload") or {})
    if (b.get("payload") or {}).get("method", "") == "tools/call":
        try:
            _audit.audit_append(inst["name"], "mcp:" + str(b.get("server")),
                         ((b.get("payload") or {}).get("params") or {}).get("name", ""),
                         st == 200 and "error" not in out)
        except Exception:
            pass
    return h._json(out, st)


# ---- agents, tasks, missions, playbooks, memory (guest-scoped) --------------
@ROUTER.get("/api/agent-tools")
def _rt_agent_tools(h):
    return h._json({"tools": AGENT_TOOLS_CATALOG})


@ROUTER.get("/api/agents")
def _rt_agents(h):
    # Roster for routing (list_agents). Capabilities only — no secrets. A
    # guest lists only what it may delegate to; ephemeral children hidden.
    guest = h._guest()
    roster = []
    for i in _instances.load_instances():
        if i["name"].startswith(("task-", "sub-")):
            continue
        if guest is not None and not guest_may_target(guest, i["name"]):
            continue
        cfg = i.get("config") or {}
        mkey = next((k for k in MODEL_KEYS if cfg.get(k)), "")
        # Backend from the set model key, not the template (which stays
        # "openrouter" after a switch to orcarouter/llama via set_model).
        backend = {v: k for k, v in PROVIDER_MODEL_KEY.items()}.get(mkey, i.get("template", ""))
        if cfg.get("LLAMA_ENDPOINT"):
            backend = "llama"
        roster.append({"name": i["name"], "template": i.get("template", ""),
                       "backend": backend, "running": _instances.is_running(i),
                       "model": cfg.get(mkey, "") if mkey else "",
                       "mcps": [n for n in (cfg.get("MCP_SERVERS", "") or "").split(",") if n]})
    return h._json({"agents": roster})


@ROUTER.get("/api/inbox")
def _rt_inbox(h):
    # EVERY user message of every chat — among guests only the orchestrator.
    guest = h._guest()
    if guest is not None and guest["name"] != ORCH_INSTANCE:
        return h._forbid()
    peek = _qs(h).get("peek", ["0"])[0] == "1"
    return h._json({"messages": inbox_since(peek=peek)})


@ROUTER.get("/api/missions")
def _rt_missions(h):
    # Guest: only its OWN missions. Admin: ?instance= or all.
    g = h._guest()
    if g is not None:
        return h._json({"missions": _missions.mission_list(g["name"])})
    inst = _qs(h).get("instance", [""])[0]
    return h._json({"missions": _missions.mission_list(inst)} if inst else {"by_instance": _missions.load_missions()})


@ROUTER.get("/api/playbooks")
def _rt_playbooks(h):
    g = h._guest()
    inst = g["name"] if g else _qs(h).get("instance", [""])[0]
    return h._json({"playbooks": _rules.pb_list(inst)})


@ROUTER.get("/api/tasks-open")
def _rt_tasks_open(h):
    g = h._guest()
    if g is not None and g.get("name") != ORCH_INSTANCE:
        return h._json({"error": "orchestrator only"}, 403)
    rows = [{"id": t.get("id"), "instance": t.get("instance"),
             "schedule": t.get("schedule", ""), "status": t.get("status", ""),
             "next_run": t.get("next_run", 0), "message": str(t.get("message", ""))[:200]}
            for t in _store.load_tasks()]
    return h._json({"tasks": rows})


@ROUTER.get("/api/history")
def _rt_history(h):
    # Guest: only runs it created or executed; orchestrator and admin: all.
    q = _qs(h)
    guest = h._guest()
    scope = guest["name"] if guest is not None and guest["name"] != ORCH_INSTANCE else None
    return h._json({"rows": _store.history_search(q.get("q", [""])[0], q.get("limit", ["20"])[0],
                                           instance=scope)})


@ROUTER.get("/api/hitl/", prefix=True)
def _rt_hitl_status(h):
    hid = _tail(h, "/api/hitl/")[0].strip()
    guest = h._guest()
    return h._json({"status": _signal_mod.hitl_status(hid, guest["name"] if guest else None)})


@ROUTER.get("/api/memory/", prefix=True)
def _rt_memory_get(h):
    # Keys may carry spaces/umlauts; a slash inside a key stays one key. A
    # guest reads only its OWN memory — the name comes from the source IP.
    seg = _tail(h, "/api/memory/")
    if len(seg) > 2:
        seg = [seg[0], "/".join(seg[1:])]
    guest = h._guest()
    inst = guest["name"] if guest else seg[0]
    if len(seg) >= 2 and seg[1]:
        return h._json({"value": _store.mem_recall(inst, seg[1])})
    return h._json(_store.mem_recall(inst))


@ROUTER.post("/api/memory/", prefix=True)
def _rt_memory_post(h):
    b = h._body()
    guest = h._guest()
    target = guest["name"] if guest else _tail(h, "/api/memory/")[0]
    key, value = b.get("key", ""), b.get("value")   # null = delete
    msg = _store.mem_store(target, key, value)
    if guest or any(i.get("name") == target for i in _instances.load_instances()):
        try:                                            # the readable mirror in /memory —
            _memfs.note_write(target, key, value)       # for real instances only, no folder per typo
            _memfs.commit(target, f"memory_store: {str(key)[:60]}")
        except Exception as e:
            print(f"[quiet] memfs note failed: {e!r}", flush=True)
    # Also store semantically; if the embedder fails the flat memory stays.
    sem = _store.sem_store(target, value, key) if value is not None else False
    if value is not None:
        if _instances.hindsight_retains(target):
            _hindsight.retain_async(target, f"{key}: {value}", ("note",))  # explicit note -> second memory
    msg += " (+semantic)" if sem else ("" if value is None else " (semantic off)")
    return h._json({"msg": msg})


@ROUTER.post("/api/memory-search")
def _rt_memory_search(h):
    b = h._body()
    guest = h._guest()
    target = guest["name"] if guest else (b.get("instance") or "")
    hits = _store.sem_search(target, b.get("query", ""), b.get("k", 5)) if target else []
    if target and _hindsight.enabled():
        seen = {x["text"] for x in hits}
        hits += [x for x in _hindsight.recall(target, b.get("query", ""), b.get("k", 5)) if x["text"] not in seen]
    return h._json({"hits": hits})


@ROUTER.post("/api/memory-reflect")
def _rt_memory_reflect(h):
    # memory_reflect tool: a reasoned answer from the instance's Hindsight
    # bank. Guests get their own bank only; the admin may name an instance.
    b = h._body()
    guest = h._guest()
    target = guest["name"] if guest else (b.get("instance") or "")
    if not target:
        return h._json({"error": "instance missing"}, 400)
    return h._json({"text": _hindsight.reflect(target, b.get("query", ""))})


@ROUTER.post("/api/task")
def _rt_task_create_guest(h):
    # create_task tool: the caller is identified by source IP and chooses the
    # TARGET, not its identity. Ephemeral children may not create tasks.
    inst = h._guest()
    body = h._body()
    if inst is None:
        return h._json({"error": "guests only"}, 403)
    if inst["name"].startswith(("task-", "sub-")):
        return h._json({"error": "ephemeral VMs may not create tasks"})
    target, terr = resolve_task_target(body.get("target"))
    message = str(body.get("message", "")).strip()
    schedule = str(body.get("schedule", "")).strip()
    model = str(body.get("model") or "").strip()[:120]   # ephemeral only
    if not message:
        return h._json({"error": "message missing"})
    if terr:
        return h._json({"error": terr})
    if not guest_may_target(inst, target):
        return h._json({"error": f"target '{target}' not allowed for this instance "
                                 "(own name, 'ephemeral', or a DELEGATE_TARGETS entry in its config)"})
    sandbox = None
    if body.get("sandbox"):
        if target != "ephemeral":
            return h._json({"error": "sandbox applies to ephemeral targets only"})
        scfg, sinternet, serr = sandbox_config(inst, body.get("sandbox"))
        if serr:
            return h._json({"error": f"sandbox: {serr}"})
        sandbox = {"cfg": scfg, "internet": sinternet}
    if body.get("wait") and not schedule:
        ok, res = _run_task_now(target, message, model, sandbox=sandbox)
        _store.history_add(target, message, res, ok, origin=inst["name"])
        return h._json({"ok": ok, "result": res})
    t = _store.add_task(target, message, schedule, model=model, sandbox=sandbox)
    return h._json({"id": t["id"], "status": t["status"], "target": target})


def _orchestrator_or_admin(h):
    g = h._guest()
    return g is None or g.get("name") == ORCH_INSTANCE


@ROUTER.post("/api/task-edit")
def _rt_task_edit(h):
    if not _orchestrator_or_admin(h):
        return h._json({"error": "orchestrator only"}, 403)
    b = h._body()
    return h._json({"result": _store.update_task(str(b.get("id") or ""), b.get("message"), b.get("schedule"))})


@ROUTER.post("/api/task-delete")
def _rt_task_delete(h):
    if not _orchestrator_or_admin(h):
        return h._json({"error": "orchestrator only"}, 403)
    tid = str(h._body().get("id") or "")

    def del_mut(tasks):
        keep = [x for x in tasks if x.get("id") != tid]
        gone = len(tasks) - len(keep)
        tasks[:] = keep
        return bool(gone), gone
    return h._json({"deleted": _store.with_tasks(del_mut), "id": tid})


@ROUTER.post("/api/playbook-add")
@ROUTER.post("/api/playbook-remove")
def _rt_playbook_edit(h):
    b = h._body()
    g = h._guest()
    inst = g["name"] if g else (b.get("instance") or "")
    if h.path.split("?", 1)[0].endswith("add"):
        r = _rules.pb_add(inst, b.get("text") or b.get("rule") or "")
        return h._json({"id": r, "added": bool(r and r != "exists"), "note": r})
    return h._json({"removed": _rules.pb_remove(inst, b.get("id") or "")})


@ROUTER.post("/api/mission-start")
@ROUTER.post("/api/mission-update")
@ROUTER.post("/api/mission-finish")
def _rt_mission_write(h):
    # Every persistent agent (its own missions) or admin. Ephemeral VMs are
    # excluded — deleted after the task, their mission would dangle.
    g = h._guest()
    if g is not None and g["name"].startswith(("task-", "sub-")):
        return h._json({"error": "ephemeral VMs may not own missions"}, 403)
    inst = g["name"] if g else ORCH_INSTANCE
    b = h._body()
    p = h.path.split("?", 1)[0]
    if p.endswith("start"):
        mid, note = _missions.mission_start(inst, b.get("goal", ""), b.get("steps") or [])
        return h._json({"id": mid, "note": note})
    if p.endswith("update"):
        return h._json({"msg": _missions.mission_update(inst, b.get("id", ""), step=b.get("step"),
                                              status=b.get("status"), result=b.get("result", ""),
                                              task_id=b.get("task_id", ""), add_step=b.get("add_step", ""),
                                              note=b.get("note", ""), target=b.get("target", ""))})
    return h._json({"msg": _missions.mission_finish(inst, b.get("id", ""), summary=b.get("summary", ""),
                                          failed=bool(b.get("failed")))})


@ROUTER.post("/api/mission-admin", admin=True)
def _rt_mission_admin(h):
    # UI: pause/resume/abort, delete, edit. Without an instance the owner is
    # resolved from the id — web UI and app only know the mission id.
    b = h._body()
    action = b.get("action", "")
    if action == "delete":
        msg = _missions.mission_delete(b.get("instance", ""), b.get("id", ""))
    elif action == "edit":
        msg = _missions.mission_edit(b.get("instance", ""), b.get("id", ""), goal=b.get("goal"),
                           steps=b.get("steps"), status=b.get("status"))
    else:
        msg = _missions.mission_admin(b.get("instance", ""), b.get("id", ""), action)
    return h._json({"msg": msg})


# ---- reports from guests: usage, audit, notify, hitl, signal, chat-log ------
def usage_report_accepted(inst, body):
    """Whose figures count: with the key proxy off, the agent's; with it on,
    the proxy's — except for a model the instance calls DIRECTLY, not through
    the proxy: a local model (LLAMA_ENDPOINT) or a claude-template instance
    (Claude Code on the host subscription). Their own report is the only one."""
    if (_settings.load_settings().get("LLM_KEY_PROXY") or "") != "1":
        return True
    if not body.get("direct"):
        return False
    return bool((inst.get("config") or {}).get("LLAMA_ENDPOINT")) or inst.get("template") == "claude"


@ROUTER.post("/api/usage")
def _rt_usage_report(h):
    # Only real guests: the instance comes from the source IP, not the body.
    # With the key proxy on, the proxy books what the upstream reports and
    # the agent's own figures are ignored (else a quiet agent has no budget).
    inst = h._guest()
    body = h._body()
    if inst is not None and usage_report_accepted(inst, body):
        _store.usage_add(inst["name"], body.get("model", ""), body.get("prompt_tokens"),
                  body.get("completion_tokens"), body.get("cost"),
                  turn=body.get("turn", ""), ms=body.get("ms"), step=body.get("step"),
                  ok=body.get("ok", True), err=body.get("err", ""))
    h.send_response(204); h.end_headers()


@ROUTER.post("/api/trace")
def _rt_trace(h):
    # Turn markers from the agent: start opens a turns row, end closes it with
    # duration, steps and outcome. Guests only, instance by IP, rate-limited.
    inst = h._guest()
    body = h._body()
    if inst is None:
        return h._forbid()
    if not _util.rate_ok(("trace", inst["name"]), 120, 300):
        return h._json({"error": "rate limit"}, 429)
    turn = str(body.get("turn") or "")[:16]
    if turn:
        if body.get("event") == "start":
            _store.turn_start(inst["name"], turn, body.get("kind") or "chat")
        elif body.get("event") == "end":
            _store.turn_end(inst["name"], turn, ms=body.get("ms"), steps=body.get("steps"),
                     outcome=body.get("outcome") or "ok", kind=body.get("kind") or "chat")
    h.send_response(204); h.end_headers()


@ROUTER.get("/api/trace/", prefix=True, admin=True)
def _rt_trace_read(h):
    # One turn as a span tree: the turn row, its LLM calls (llm_usage) and its
    # tool calls (audit lines with that turn id), each with duration. Without
    # ?turn= the last 50 turns of the instance.
    nm = re.sub(r"[^a-zA-Z0-9_-]", "", _tail(h, "/api/trace/")[0])
    q = _qs(h)
    turn = re.sub(r"[^a-zA-Z0-9_-]", "", q.get("turn", [""])[0])[:16]
    if not turn:
        try:
            limit = max(1, min(int(q.get("limit", ["50"])[0]), 500))
        except ValueError:
            limit = 50
        return h._json({"instance": nm, "turns": _store.turns_read(nm, limit=limit)})
    t = _store.turn_trace(nm, turn)
    # audit_read is newest-first; the file order is the call order (ts has
    # only seconds, so a stable sort on ts alone would swap calls of one second)
    tools = [e for e in reversed(_audit.audit_read(nm, limit=_audit.AUDIT_MAX_LINES)) if e.get("turn") == turn]
    return h._json({"instance": nm, "turn": t["turn"], "llm": t["llm"], "tools": tools})


@ROUTER.post("/api/audit")
def _rt_audit_report(h):
    inst = h._guest()
    body = h._body()
    if inst is not None:   # only log real guests, silently discard otherwise
        try:
            _audit.audit_append(inst["name"], body.get("tool", ""), body.get("target", ""),
                         body.get("ok", True), err=body.get("err", ""),
                         result=body.get("result", ""), turn=body.get("turn", ""),
                         ms=body.get("ms"))
        except Exception:
            pass
    h.send_response(204); h.end_headers()


@ROUTER.post("/api/notify")
def _rt_notify(h):
    body = h._body()
    inst = h._guest()
    if inst is not None and not tool_allowed(inst, "notify"):
        return h._json({"error": "notify not allowed for this instance"}, 403)
    nm = inst["name"] if inst else "admin"
    text = body.get("body") or body.get("message", "")
    nid, note = _notify.notify_add(nm, body.get("title", ""), text, link=("chat:" + nm) if inst else "")
    if nid and inst:
        # The click on a notification lands in the instance's task chat — so
        # the notification's own text goes there too. Until now a report an
        # agent sent only via notify (the Saddler review) was nowhere to be
        # found after the click: the chat held "(max tool steps reached)".
        try:
            rtitle = _gateway.redact_secrets(str(body.get("title") or "").strip()[:120])[0]
            rtext = _gateway.redact_secrets(str(text)[:4000])[0]
            chat_log_append(nm, "", "", f"🔔 {rtitle}\n\n{rtext}", kind="task")
        except Exception as e:
            print(f"[quiet] notify -> task chat failed: {e!r}", flush=True)
    try:
        # The WHY travels along ("empty" / "rate limit: …").
        _audit.audit_append(nm, "notify", (body.get("title") or "")[:60], bool(nid), err="" if nid else str(note))
    except Exception:
        pass
    return h._json({"id": nid, "note": note}, 200 if nid else 429)


@ROUTER.post("/api/notifications/read", admin=True)
def _rt_notifications_read(h):
    body = h._body()
    n = _notify.notif_clear() if body.get("clear") else _notify.notif_mark_read(body.get("id"), bool(body.get("all")))
    return h._json({"marked": n})


@ROUTER.post("/api/hitl")
def _rt_hitl_create(h):
    body = h._body()
    inst = h._guest()
    hid = _signal_mod.hitl_create(inst["name"] if inst else "admin", str(body.get("tool", ""))[:40],
                      str(body.get("target", ""))[:200])
    return h._json({"id": hid})


@ROUTER.post("/api/signal")
def _rt_signal_send(h):
    # Recipient checked against ALLOWED_SENDERS, bot number from settings —
    # the VM knows neither.
    body = h._body()
    inst = h._guest()
    if inst is not None and not tool_allowed(inst, "send_signal"):
        return h._json({"ok": False, "note": "send_signal not allowed for this instance"}, 403)
    ok, note = _signal_mod.signal_send(body.get("text") or body.get("message"), body.get("to"))
    try:
        _audit.audit_append(inst["name"] if inst else "admin", "send_signal", (body.get("to") or "default"), ok)
    except Exception:
        pass
    return h._json({"ok": ok, "note": note}, 200 if ok else 400)


@ROUTER.post("/api/chat-log")
def _rt_chat_log(h):
    # A Signal turn into the shared chat history — and, through the inbox, a
    # request to the orchestrator. Only a Signal-transport guest may file one
    # (a web or voice VM has nobody typing on Signal), rate-limited; the
    # inbox marks it as relayed, so an agent cannot pose as the user.
    inst = h._guest()
    body = h._body()
    if inst is None or (inst.get("config") or {}).get("TRANSPORT", "signal") != "signal":
        return h._forbid()
    if not _util.rate_ok(("chat-log", inst["name"]), 60, 300):
        return h._json({"error": "rate limit"}, 429)
    if inst is not None:
        try:
            chat_log_append(inst["name"], body.get("sender", ""), body.get("user", ""), body.get("reply", ""))
        except Exception as e:
            print(f"[quiet] chat_log_append failed: {e!r}", flush=True)
        try:
            orchestrator_ping()   # Signal message -> orchestrator immediately
        except Exception:
            pass
    h.send_response(204); h.end_headers()


# ---- voice ----------------------------------------------------------------
@ROUTER.post("/api/stt")
@ROUTER.post("/api/tts")
def _rt_voice(h):
    # The voice service listens on loopback; the manager is the only door and
    # passes raw audio / WAV through unchanged.
    p = h.path.split("?", 1)[0]
    payload = h._raw(BODY_MAX_AUDIO)
    if p == "/api/tts":
        # Read-aloud filter + voice/speed from the settings (explicit client values win).
        try:
            b = json.loads(payload or b"{}")
            b["text"] = _voice.speakable_text(b.get("text", ""))
            st = _settings.load_settings()
            if st.get("TTS_VOICE") and not b.get("voice"):
                b["voice"] = st["TTS_VOICE"]
            if st.get("TTS_SPEED") and not b.get("speed"):
                b["speed"] = float(str(st["TTS_SPEED"]).replace(",", "."))
            payload = json.dumps(b).encode()
        except (ValueError, TypeError):
            pass
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{_settings.VOICE_PORT}{p[len('/api'):]}", data=payload,
                                     method="POST", headers={"Content-Type": h.headers.get(
                                         "Content-Type", "application/octet-stream")})
        with urllib.request.urlopen(req, timeout=180) as r:
            data = r.read()
            ct = r.headers.get("Content-Type", "application/json")
        code = 200
        if p == "/api/stt":
            try:
                j = json.loads(data)
                g = h._guest()
                _voice.stt_remember(j.get("text", ""), j.get("seconds"), g["name"] if g else h.client_address[0],
                             audio=payload, ctype=h.headers.get("Content-Type", ""))
            except (ValueError, TypeError):
                pass
    except urllib.error.HTTPError as e:
        data, ct, code = e.read(), "application/json", e.code
    except Exception as e:
        data = json.dumps({"error": f"voice service unreachable: {e!r}"}).encode()
        ct, code = "application/json", 503
    h.send_response(code)
    h.send_header("Content-Type", ct)
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)


# ---- katfs (guest: own share only; admin: browser) -------------------------
def _katfs_answer(h, op, share, path, *extra):
    try:
        st, ct, data = _katfs.katfs_proxy_fs(op, share, path, *extra)
    except urllib.error.HTTPError as e:
        st, ct, data = e.code, "application/json", e.read()
    except Exception as e:
        st, ct, data = 503, "application/json", json.dumps({"error": str(e)}).encode()
    return st, ct, data


@ROUTER.get("/api/katfs/ls")
@ROUTER.get("/api/katfs/read")
def _rt_katfs_guest_fs(h):
    inst = h._guest()
    if inst is None:
        return h._json({"error": "guests only"}, 403)
    op = "ls" if h.path.split("?", 1)[0].endswith("/ls") else "read"
    st, ct, data = _katfs_answer(h, op, _katfs.katfs_share_for(inst), _qs(h).get("path", ["."])[0])
    h._send(data, ct, st)


@ROUTER.post("/api/katfs/write")
@ROUTER.post("/api/katfs/delete")
def _rt_katfs_guest_write(h):
    inst = h._guest()
    if inst is None:
        return h._json({"error": "guests only"}, 403)
    q = _qs(h)
    path, share = q.get("path", [""])[0], _katfs.katfs_share_for(inst)
    ln = int(h.headers.get("Content-Length", 0) or 0)
    if h.path.split("?", 1)[0].endswith("/write"):
        if ln > _katfs.KATFS_MAX_WRITE:
            return h._json({"error": "too large"}, 413)
        st, ct, data = _katfs_answer(h, "write", share, path, False, h.rfile.read(ln) if ln else b"")
    else:
        if ln:
            h.rfile.read(ln)
        st, ct, data = _katfs_answer(h, "delete", share, path, q.get("recursive", ["0"])[0] == "1", None)
    h._send(data, ct, st)


@ROUTER.get("/api/katfs/zip", admin=True)
def _rt_katfs_zip(h):
    q = _qs(h)
    root, share = q.get("path", ["."])[0], q.get("share", [""])[0]
    try:
        data, stats = _katfs.katfs_zip(share, root)
    except Exception as e:
        return h._json({"error": str(e)}, 502)
    leaf = os.path.basename(root.rstrip("/")) if root not in (".", "") else "katfs"
    h.send_response(200)
    h.send_header("Content-Type", "application/zip")
    h.send_header("Content-Disposition", f'attachment; filename="{_util.download_name(leaf, "katfs")}.zip"')
    h.send_header("X-Katfs-Files", str(stats.get("files", 0)))
    h.send_header("Content-Length", str(len(data)))
    h.end_headers(); h.wfile.write(data)


@ROUTER.get("/api/katfs/browse", admin=True)
@ROUTER.get("/api/katfs/file", admin=True)
def _rt_katfs_browse(h):
    q = _qs(h)
    path, share = q.get("path", ["."])[0], q.get("share", [""])[0]
    op = "ls" if "/browse" in h.path else "read"
    st, ct, data = _katfs_answer(h, op, share, path)
    if op == "read" and st == 200:
        # Images/text viewable in the new tab, otherwise download.
        ct = mimetypes.guess_type(path)[0] or "application/octet-stream"
        disp = "attachment" if q.get("dl", [""])[0] == "1" else "inline"
        h.send_response(200)
        h.send_header("Content-Type", ct)
        h.send_header("Content-Disposition", f'{disp}; filename="{_util.download_name(os.path.basename(path))}"')
        h.send_header("Content-Length", str(len(data)))
        h.end_headers(); h.wfile.write(data)
        return
    h._send(data, ct, st)


@ROUTER.get("/api/katfs/status", admin=True)
def _rt_katfs_status(h):
    return h._json(_katfs.katfs_status())


@ROUTER.get("/api/browse", admin=True)
def _rt_host_browse(h):
    q = _qs(h)
    return h._json(_browse.list_dirs(q.get("path", ["/"])[0], q.get("hidden", [""])[0] == "1"))


# ---- admin reads --------------------------------------------------------------
@ROUTER.get("/api/usage/", prefix=True, admin=True)
def _rt_usage_for(h):
    nm = re.sub(r"[^a-zA-Z0-9_-]", "", _tail(h, "/api/usage/")[0])
    try:
        since = int(_qs(h).get("since", ["0"])[0] or 0)
    except ValueError:
        since = 0
    return h._json(_store.usage_for(nm, since))


@ROUTER.get("/api/policy", admin=True)
def _rt_policy(h):
    return h._json({"instances": [effective_policy(i) for i in _instances.load_instances()]})


@ROUTER.get("/api/audit/", prefix=True, admin=True)
def _rt_audit_read(h):
    nm = re.sub(r"[^a-zA-Z0-9_-]", "", _tail(h, "/api/audit/")[0])
    return h._json({"instance": nm, "events": _audit.audit_read(nm, limit=1000)})


@ROUTER.get("/api/models")
def _rt_models(h):
    return h._json({"curated": sorted(_models.load_curated())})


@ROUTER.get("/api/plugins")
def _rt_plugins(h):
    return h._json({"plugins": _plugins.list_plugins()})


@ROUTER.get("/api/changelog", admin=True)
def _rt_changelog(h):
    return h._json({"text": _about.load_changelog()})


@ROUTER.get("/api/security", admin=True)
def _rt_security(h):
    return h._json({"issues": _about.load_security()})


@ROUTER.get("/api/secret-keys", admin=True)
def _rt_secret_keys(h):
    # Names only, never values; `sources` says where a key lives (the store
    # file, editable here, or the settings, edited in the Settings tab).
    store = _secrets.load_secrets_file()
    keys = sorted(_secrets.secret_store().keys())
    return h._json({"keys": keys, "sources": {k: ("store" if k in store else "settings") for k in keys}})


@_msg_route("POST", "/api/update")
def _rt_update(h):
    return _about.update_start()


@_msg_route("POST", "/api/secret-store")
def _rt_secret_store_set(h):
    b = h._body()
    return _secrets.secret_set(b.get("name", ""), b.get("value", ""))


@_msg_route("POST", "/api/secret-store/", prefix=True)
def _rt_secret_store_delete(h):
    parts = h.path.split("?", 1)[0].strip("/").split("/")
    if len(parts) == 4 and parts[3] == "delete":
        return _secrets.secret_delete(parts[2])
    return "unknown"


@ROUTER.get("/api/secret-policy", admin=True)
def _rt_secret_policy(h):
    return h._json(_secrets.load_secret_policy())


@ROUTER.get("/api/mcps", admin=True)
def _rt_mcps(h):
    return h._json(_mcp.load_mcps())


@ROUTER.get("/api/openrouter-models", admin=True)
def _rt_openrouter_models(h):
    return h._json(_models.openrouter_models("refresh=1" in h.path, "tools=1" in h.path, "relevant=1" in h.path))


def _since_wait(q):
    try:
        since = int(q.get("since", ["0"])[0] or 0)
        wait = min(30.0, max(0.0, float(q.get("wait", ["25"])[0] or 0)))
    except ValueError:
        since, wait = 0, 0.0
    return since, wait


@ROUTER.get("/api/chats", admin=True)
def _rt_chats(h):
    q = _qs(h)
    if "since" in q or "wait" in q:
        rev, chats = wait_chats(*_since_wait(q))
        return h._json({"rev": rev, "chats": chats, "tombstones": load_tombstones()})
    return h._json(load_chats())


@ROUTER.get("/api/notifications", admin=True)
def _rt_notifications(h):
    q = _qs(h)
    if "since" in q or "wait" in q:
        rev, notifs = _notify.wait_notifs(*_since_wait(q))
        unread = sum(1 for n in _notify.load_notifications() if not n.get("read"))
        return h._json({"rev": rev, "notifications": notifs, "unread": unread})
    lst = _notify.load_notifications()
    return h._json({"notifications": lst, "unread": sum(1 for n in lst if not n.get("read"))})


# ---- admin writes: the {"msg": …} family ---------------------------------------
@ROUTER.post("/api/iroh", admin=True)
def _rt_iroh(h):
    b = h._body()
    act = b.get("action")
    if act == "add":
        ok, msg = _irohgw.allow_add(b.get("id", ""), b.get("label", ""))
    elif act == "remove":
        ok, msg = _irohgw.allow_remove(b.get("id", ""))
    else:
        ok, msg = False, "unknown action"
    return h._json({"ok": ok, "msg": msg, **_irohgw.status()}, 200 if ok else 400)


@ROUTER.post("/api/prompts", admin=True)
def _rt_prompts(h):
    b = h._body()
    msg = _rules.prompt_delete(b.get("name", "")) if b.get("delete") else _rules.prompt_upsert(b.get("name", ""), b.get("text", ""))
    return h._json({"msg": msg})


@ROUTER.post("/api/plugins", admin=True)
@ROUTER.post("/api/plugins/", prefix=True, admin=True)
def _rt_plugins_manage(h):
    pp = h.path.split("?", 1)[0]
    ln = int(h.headers.get("Content-Length", 0) or 0)
    raw_body = h.rfile.read(ln) if ln else b""
    if ln > _plugins.PLUGIN_MAX_BYTES:
        return h._json({"error": "file too large (max 5 MB)"})
    try:
        b = json.loads(raw_body or b"{}")
    except ValueError:
        b = {}
    parts = pp.strip("/").split("/")
    if len(parts) == 4 and parts[3] == "delete":
        return h._json({"msg": "deleted" if _plugins.plugin_delete(parts[2]) else "not found"})
    if len(parts) == 4 and parts[3] == "pin":
        sha = _plugins.plugin_pin(parts[2])
        return h._json({"msg": "approved" if sha else "not found", "sha": (sha or "")[:12]})
    if pp == "/api/plugins/new":
        err = _plugins.plugin_write_py(b.get("name", ""), _plugins.PLUGIN_BOILERPLATE)
        return h._json({"error": err} if err else {"msg": "created"})
    name = b.get("name", "")
    if b.get("kind") == "zip":
        try:
            raw = base64.b64decode(b.get("data_b64", ""))
        except Exception:
            raw = b""
        err = _plugins.plugin_write_zip(name, raw)
    else:
        code = b.get("code")
        if code is None and b.get("data_b64"):
            code = base64.b64decode(b.get("data_b64", "")).decode("utf-8", "replace")
        err = _plugins.plugin_write_py(name, code or _plugins.PLUGIN_BOILERPLATE)
    return h._json({"error": err} if err else {"msg": "saved"})


@_msg_route("POST", "/api/settings")
def _rt_settings_save(h):
    return _settings.save_settings(h._body())


@_msg_route("POST", "/api/security")
def _rt_security_save(h):
    return _about.save_security(h._body().get("issues") or [])


@_msg_route("POST", "/api/gateway")
def _rt_gateway_toggle(h):
    # {"chat": "<id>", "on": true}
    b = h._body()
    cid = str(b.get("chat") or "")
    if not cid:
        return "chat missing"

    def gw_mut(d, _cid=cid, _on=bool(b.get("on"))):
        if _on:
            d["chats"][_cid] = True
        else:
            d["chats"].pop(_cid, None)
    _gateway.with_gateway(gw_mut)
    return f"gateway {'on' if b.get('on') else 'off'} for {cid}"


@_msg_route("POST", "/api/models")
def _rt_models_save(h):
    return _models.save_curated(h._body().get("curated") or [])


@_msg_route("POST", "/api/chats")
def _rt_chats_merge(h):
    n = merge_chats(h._body(default=[]))
    try:
        orchestrator_ping()   # new app/web message -> orchestrator immediately
    except Exception:
        pass
    return f"{n} chats saved" if n >= 0 else "error while saving"


@_msg_route("POST", "/api/tasks")
def _rt_tasks_create(h):
    b = h._body()
    target, terr = resolve_task_target(b.get("instance"))
    if not b.get("instance") or not b.get("message"):
        return "instance/message missing"
    if terr:
        return terr
    t = _store.add_task(target, b.get("message", ""), b.get("schedule", ""))
    return f"task {t['id']} created ({t['status']})"


@_msg_route("POST", "/api/tasks/", prefix=True)
def _rt_tasks_admin(h):
    parts = h.path.split("?", 1)[0].strip("/").split("/")
    if len(parts) != 4:
        return "unknown"
    tid, action = parts[2], parts[3]
    if action == "delete":
        _store.with_tasks(lambda ts, _tid=tid: (True, ts.__setitem__(slice(None), [x for x in ts if x["id"] != _tid])))
        return f"task {tid} deleted"
    if action == "update":
        b = h._body()
        inst_new, terr = (resolve_task_target(b.get("instance")) if b.get("instance") else (None, ""))
        return terr or _store.update_task(tid, b.get("message"), b.get("schedule"), instance=inst_new)
    if action == "run":
        return run_task_now(tid)
    return "unknown"


def run_task_now(tid):
    """Queue a task for the worker's next tick: a scheduled one keeps its
    schedule (only this run is pulled forward), a one-off is set pending
    again, whatever its last outcome. A running one is left alone."""
    out = {"msg": "unknown"}

    def mut(tasks):
        t = next((x for x in tasks if x["id"] == tid), None)
        if t is None:
            return False, None
        if t.get("status") == "running":
            out["msg"] = f"task {tid} is running already"
            return False, None
        if t.get("schedule"):
            t["next_run"] = int(time.time())
        else:
            t["status"] = "pending"
        t.pop("target_warned", None)
        out["msg"] = f"task {tid} queued — runs within a minute"
        return True, None
    _store.with_tasks(mut)
    return out["msg"]


@_msg_route("POST", "/api/secret-policy")
def _rt_secret_policy_save(h):
    return _secrets.save_secret_policy(h._body())


@_msg_route("POST", "/api/mcps")
def _rt_mcps_upsert(h):
    b = h._body()
    return _mcp.upsert_mcp(b.get("name", ""), b.get("description", ""), b.get("command", ""), b.get("args", []), b.get("env"))


@_msg_route("POST", "/api/mcps/", prefix=True)
def _rt_mcps_delete(h):
    parts = h.path.split("?", 1)[0].strip("/").split("/")
    if len(parts) == 4 and parts[3] == "delete":
        return _mcp.delete_mcp(re.sub(r"[^a-z0-9_-]", "", parts[2].lower()))
    return "unknown"


@_msg_route("POST", "/api/personas")
def _rt_personas_upsert(h):
    b = h._body()
    return upsert_persona(b.get("name", ""), b.get("prompt", ""), b.get("tools"), b.get("model"))


@_msg_route("POST", "/api/personas/", prefix=True)
def _rt_personas_delete(h):
    parts = h.path.split("?", 1)[0].strip("/").split("/")
    if len(parts) == 4 and parts[3] == "delete":
        return delete_persona(re.sub(r"[^a-z0-9_-]", "", parts[2].lower()))
    return "unknown"


@_msg_route("POST", "/api/skills")
def _rt_skills_upsert(h):
    b = h._body()
    return upsert_skill(b.get("name", ""), b.get("description", ""), b.get("content", ""))


@ROUTER.post("/api/skill-proposals")
def _rt_skill_propose(h):
    # A guest files a skill proposal (distilled after a long successful turn,
    # or deliberately via propose_skill). Instance by IP, rate-limited,
    # linted; it waits for approval in the Skills tab.
    inst = h._guest()
    if inst is None:
        return h._forbid()
    if not _util.rate_ok(("skill-proposal", inst["name"]), 10, 300):
        return h._json({"error": "rate limit"}, 429)
    b = h._body()
    pid, why = proposal_add(inst["name"], b.get("name", ""), b.get("description", ""), b.get("content", ""),
                            turn=b.get("turn", ""), note=b.get("note", ""))
    if pid is None:
        return h._json({"error": why}, 400)
    return h._json({"id": pid, "msg": "proposed — waiting for approval in the Skills tab"})


@ROUTER.get("/api/skill-proposals", admin=True)
def _rt_skill_proposals(h):
    return h._json({"proposals": [p for p in load_proposals() if p.get("status") == "proposed"]})


@_msg_route("POST", "/api/skill-proposals/", prefix=True)
def _rt_skill_proposal_decide(h):
    parts = h.path.split("?", 1)[0].strip("/").split("/")
    if len(parts) != 4 or parts[3] not in ("approve", "discard"):
        return "unknown"
    return proposal_decide(re.sub(r"[^a-f0-9]", "", parts[2]), parts[3] == "approve")


@ROUTER.post("/api/sessions-search")
def _rt_sessions_search(h):
    # Guests search their own sessions; the orchestrator sees every
    # instance's (it delegates across them); admins may pass instance.
    b = h._body()
    inst = h._guest()
    if inst is not None:
        if not _util.rate_ok(("sessions-search", inst["name"]), 60, 300):
            return h._json({"error": "rate limit"}, 429)
        scope = None if inst["name"] == ORCH_INSTANCE else inst["name"]
        if scope is None and b.get("instance"):
            scope = str(b.get("instance"))[:80]
    else:
        scope = str(b.get("instance") or "")[:80] or None
    try:
        limit = max(1, min(int(b.get("limit", 10)), 50))
    except (TypeError, ValueError):
        limit = 10
    return h._json({"hits": sessions_search(str(b.get("q") or b.get("query") or ""), instance=scope, limit=limit)})


@_msg_route("POST", "/api/skills/", prefix=True)
def _rt_skills_delete(h):
    parts = h.path.split("?", 1)[0].strip("/").split("/")
    if len(parts) == 4 and parts[3] == "delete":
        return delete_skill(re.sub(r"[^a-z0-9_-]", "", parts[2].lower()))
    return "unknown"


@_msg_route("POST", "/api/ha-alias", admin=False)
def _rt_ha_alias(h):
    # Guest teaches HA a spoken-name alias; the HA token stays on the host.
    g = h._guest()
    if g is not None and not tool_allowed(g, "ha_learn_alias"):
        return "ha_learn_alias not allowed for this instance"
    b = h._body()
    return _haalias.learn_alias(b.get("spoken", ""), b.get("entity", ""))


@_msg_route("POST", "/api/ha-control", admin=False)
def _rt_ha_control(h):
    # Deterministic voice control: matched server-side, no LLM in the loop.
    g = h._guest()
    if g is not None and not tool_allowed(g, "ha_control"):
        return "ha_control not allowed for this instance"
    b = h._body()
    return _haalias.control(b.get("spoken", ""), b.get("action", ""))


@_msg_route("POST", "/api/create")
def _rt_instance_create(h):
    body = h._body()
    cfg = body.get("config", {}) or {}
    mcps = [str(m) for m in (body.get("mcps") or []) if m]
    if mcps:
        cfg["MCP_SERVERS"] = ",".join(mcps)
    # Tool allowlist only as a real subset (all selected -> omit = all).
    tools = [t for t in (body.get("tools") or []) if t in AGENT_TOOL_NAMES]
    if tools and set(tools) != AGENT_TOOL_NAMES:
        cfg["AGENT_TOOLS"] = ",".join(tools)
    return create_instance(body.get("name", ""), body.get("template", ""), cfg,
                           body.get("mounts", []), internet=body.get("internet", True))


def _set_config_key(name, key, val):
    """Set/delete a single config key (secrets stay out — broker only)."""
    inst = next((i for i in _instances.load_instances() if i["name"] == name), None)
    if not inst:
        return "unknown"
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,40}", key) or key in _settings.NEVER_PERSIST:
        return f"error: key '{key}' not allowed"
    if key == "MCP_SERVERS" and _instances.mcp_servers_error(val):
        return "error: " + _instances.mcp_servers_error(val)
    cfg = inst.setdefault("config", {})
    if val in ("", None):
        cfg.pop(key, None)
    else:
        cfg[key] = str(val)
    _instances.save_instance(inst)
    return (f"{key} " + ("removed" if val in ("", None) else f"= {val}")
            + (" (applies after stop/start)" if _instances.is_running(inst) else ""))


@ROUTER.get("/api/mounts")
def _rt_mounts(h):
    # The guest's reconciler fetches ITS list here, identified by source IP,
    # instead of reading .fcmnt/<name>/desired.list off the shared workspace
    # where any other VM could write it (and have this one mount a folder
    # over /bin). Admins may ask for an instance's list with ?instance=.
    inst = h._guest()
    if inst is None:
        name = _qs(h).get("instance", [""])[0]
        inst = next((i for i in _instances.load_instances() if i["name"] == name), None)
        if inst is None:
            return h._json({"error": "instance?"}, 404)
    return desired_lines(inst).encode(), "text/plain; charset=utf-8"


@_msg_route("POST", "/api/instances/", prefix=True)
def _rt_instance_action(h):
    parts = h.path.split("?", 1)[0].strip("/").split("/")
    if len(parts) != 4:
        return "unknown"
    name, action = parts[2], parts[3]
    if action == "delete":
        return delete_instance(name)
    if action == "mounts":
        return set_mounts(name, h._body().get("mounts", []))
    if action == "internet":
        return set_internet(name, bool(h._body().get("on", True)))
    if action == "tools":
        return set_instance_tools(name, h._body().get("tools") or [])
    if action == "config":
        b = h._body()
        return _set_config_key(name, str(b.get("key", "")).strip(), b.get("value", ""))
    if action == "persist":
        return set_persist_disk(name, bool(h._body().get("on")))
    if action == "diskreset":
        return reset_upper(name)
    if action == "model":
        return set_model(name, h._body().get("model", ""))
    inst = next((i for i in _instances.load_instances() if i["name"] == name), None)
    if not inst:
        return "unknown"
    if action == "restart":       # stop/start: picks up a rebuilt image
        stop(inst)
        return start(inst)
    if action == "start":
        return start(inst)
    if action == "stop":
        return stop(inst)
    return "??"



def migrate_mcp_config_out_of_instances():
    """MCP_CONFIG contained the substituted secrets in plain text. The server
    names are its keys, so they can be lifted losslessly into MCP_SERVERS; the
    secrets needed for that are granted to the instance specifically, so nothing
    that worked before stops working."""
    pol = _secrets.load_secret_policy()
    by_inst = pol.setdefault("by_instance", {})
    touched = False
    for inst in _instances.load_instances():
        cfg = inst.get("config") or {}
        if "MCP_CONFIG" not in cfg:
            continue
        blob = cfg.get("MCP_CONFIG")
        try:
            servers = json.loads(blob).get("mcpServers", {})
            names = sorted(servers.keys())
        except (ValueError, AttributeError):
            names = []
        if not blob:
            names = []          # empty remnant from old setups — just clean up
        if names:
            cfg["MCP_SERVERS"] = ",".join(names)
            need = _mcp.mcp_required_secrets(names)
            if need:
                cur = set(by_inst.get(inst["name"], []))
                if need - cur:
                    by_inst[inst["name"]] = sorted(cur | need)
                    touched = True
        cfg.pop("MCP_CONFIG", None)
        try:
            _instances.save_instance(inst)
            print(f"[migrate] {inst['name']}: MCP_CONFIG -> MCP_SERVERS={','.join(names) or '-'}"
                  f"{' + Policy ' + ','.join(sorted(_mcp.mcp_required_secrets(names))) if names else ''}",
                  flush=True)
        except OSError as e:
            print(f"[migrate] {inst['name']}: {e}", flush=True)
    if touched:
        _secrets.save_secret_policy(pol)


def migrate_secrets_out_of_instances():
    """One-time cleanup of the legacy state: instance JSONs that still carry an
    API key lose it here. Since the rework the agent fetches it via the broker;
    a key in the instance file would only be a copy that travels onto every
    config disk. Runs as root, who owns the files."""
    for inst in _instances.load_instances():
        cfg = inst.get("config") or {}
        hit = [k for k in _settings.SECRET_PARAMS if k in cfg]
        if not hit:
            continue
        for k in hit:
            cfg.pop(k)
        try:
            _instances.save_instance(inst)
            print(f"[migrate] {inst['name']}: {', '.join(hit)} removed", flush=True)
        except OSError as e:
            print(f"[migrate] {inst['name']}: {e}", flush=True)


def harden_files(base=None):
    """Chats, audit, missions, tasks, history: written by root, readable by
    root. Nothing else on the host needs them (the operator reads through
    the UI); the guests' folders keep their own owner and mode."""
    base = base or _paths.BASE
    n = 0
    try:
        for f in os.listdir(base):
            p = os.path.join(base, f)
            if os.path.isfile(p) and f.endswith((".json", ".jsonl", ".db", ".db-wal", ".db-shm", ".txt")):
                os.chmod(p, 0o600); n += 1
        idir = os.path.join(base, "instances")       # instance JSONs: operator-readable
        if os.path.isdir(idir):
            for f in os.listdir(idir):
                if f.endswith(".json"):
                    os.chmod(os.path.join(idir, f), 0o640)
                    if os.geteuid() == 0:
                        os.chown(os.path.join(idir, f), 0, _host.ADMIN_GID)
        ad = os.path.join(base, "audit")
        if os.path.isdir(ad):
            os.chmod(ad, 0o700)
            for f in os.listdir(ad):
                os.chmod(os.path.join(ad, f), 0o600); n += 1
    except OSError as e:
        print(f"[quiet] harden_files: {e!r}", flush=True)
    return n


if __name__ == "__main__":
    print(f"kAIm56 on http://{_host.LISTEN[0]}:{_host.LISTEN[1]}  (auth={'on' if _auth.PW else 'OFF'})",
          flush=True)
    os.umask(0o077)                  # new files are root's; the few others read get a mode below
    harden_files()
    if ensure_guest_user():
        _memfs.OWNER = (GUEST_UID, _host.ADMIN_GID)
        own_guest_dir(AGENT_ROOT, 0o755)
        own_guest_dir(FCMNT_ROOT, 0o755)
    retire_root_export()
    migrate_secrets_out_of_instances()
    migrate_mcp_config_out_of_instances()
    threading.Thread(target=_task_worker, daemon=True).start()
    threading.Thread(target=_signal_mod._signal_receiver, daemon=True).start()
    ThreadingHTTPServer(_host.LISTEN, H).serve_forever()
