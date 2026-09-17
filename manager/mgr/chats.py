# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Chat history shared with the app and the web UI: chats.json with tombstones and a revision long-poll, the merge from the app, the chat log per turn, the orchestrator inbox watermark, voice sessions.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import json
import os
import re
import threading
import time

from mgr import hindsight as _hindsight
from mgr import instances as _instances
from mgr import memfs as _memfs
from mgr import paths as _paths


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
