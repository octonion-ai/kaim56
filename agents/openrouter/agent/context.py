# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""The conversation context: the history and its trimming and summarization, what is injected per turn (memory recall, playbooks, memory index, time, missions), prompt templates, and branches (side questions in inherited context).

Part of the openrouter agent package (runs inside the VM): no import from the package root. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import datetime
import json
import os
import time

from . import config as _config
from . import llm as _llm
from . import mgrclient as _mgrclient


# --- 2) context summarization ----------------------------------------------
SUMMARY_TAG = "[Summary]"
CTX_SUMMARY = os.environ.get("CTX_SUMMARY", "1") != "0"
CTX_PRESERVE_RECENT = int(os.environ.get("CTX_PRESERVE_RECENT", "10"))
SUMMARIZE_PROMPT = (
    "You summarize a conversation history. Produce a concise, structured "
    "summary in bullet points. Do NOT answer conversationally and do NOT "
    "address the user. Include: topics and questions covered; important tool calls "
    "and their results; facts, data and code that were shared; open points; key "
    "insights. Write in the third person. Do not assume that tools "
    "failed unless explicitly stated.")


def _msg_text(m):
    c = m.get("content")
    if isinstance(c, list):   # vision content -> only the text parts
        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c or ""


def _summarize(msgs, prior=""):
    """Condense a message list (conversation, without system blocks) into a short
    bullet-point summary. If the call fails -> '' (the caller then does
    the old discard behavior)."""
    lines = []
    for m in msgs:
        role = m.get("role")
        txt = _msg_text(m)
        if role == "tool":
            lines.append(f"[Tool result] {txt[:1500]}")
        elif role == "assistant":
            tcs = m.get("tool_calls")
            if tcs:
                names = ", ".join(t.get("function", {}).get("name", "?") for t in tcs)
                lines.append(f"[Assistant called tools: {names}] {txt[:800]}")
            else:
                lines.append(f"[Assistant] {txt[:1500]}")
        elif role == "user":
            lines.append(f"[User] {txt[:1500]}")
    joined = "\n".join(lines)
    if prior:
        joined = f"Prior summary:\n{prior}\n\nNew messages:\n{joined}"
    msg = _llm.or_chat([{"role": "system", "content": SUMMARIZE_PROMPT},
                   {"role": "user", "content": joined}], [])
    out = (msg.get("content") or "").strip()
    return "" if out.startswith("⚠") else out   # an error message does not count



_history = [{"role": "system", "content": _config.SYSTEM}]

# Semantic long-term memory: instead of dumping ALL facts into the prompt on the
# first turn (that grows with the memory and costs every turn), the agent
# fetches only the content-nearest notes per question. Short-term is
# _history (this conversation), long-term lives semantically in the manager.
RECALL_TAG = "[Memory]"
RECALL_K = 4
RECALL_MAX_CHARS = int(os.environ.get("RECALL_MAX_CHARS", "1200"))   # A-3: token budget for the injected recall block
# Threshold for multilingual-e5: relevant hits sit ~0.82+, thematically
# unrelated ones ~0.76. 0.78 separates cleanly. Tunable if too strict/loose.
RECALL_MIN = 0.78


def _recall(user_message):
    """Replace the memory block in _history with the long-term notes matching
    THIS question. Exactly ONE such block remains, fresh each
    turn; /reset clears it too. If the search fails, this turn simply has
    no long-term context — the notes stay stored."""
    _history[:] = [m for m in _history
                   if not (m.get("role") == "system"
                           and str(m.get("content", "")).startswith(RECALL_TAG))]
    try:
        body = _mgrclient._mgr(_mgrclient._manager_base(), "/api/memory-search",
                    {"query": user_message, "k": RECALL_K}, timeout=8)
        hits = [h for h in json.loads(body).get("hits", [])
                if h.get("score", 0) >= RECALL_MIN]
    except Exception:
        hits = []
    # A-3: don't repeat what the model already has this turn. The memory index
    # (MEMORY.md head) and the playbooks are injected too and often carry the
    # same note; drop a recall hit whose text is already there, drop exact
    # duplicates between hits, and cap the block by a char/token budget.
    already = "\n".join(str(m.get("content", "")) for m in _history
                        if m.get("role") == "system"
                        and str(m.get("content", "")).startswith((MEMINDEX_TAG, PLAYBOOK_TAG)))
    lines, seen, used = [], set(), 0
    for h in hits:
        t = str(h.get("text", "")).strip()
        key = " ".join(t.lower().split())
        if not t or key in seen or key in " ".join(already.lower().split()):
            continue
        if used + len(t) > RECALL_MAX_CHARS:
            break
        seen.add(key); used += len(t); lines.append(f"- {t}")
    if lines:
        block = (RECALL_TAG + " Relevant notes from earlier sessions "
                 "(use them when they fit the question):\n" + "\n".join(lines))
        _history.append({"role": "system", "content": block})


PLAYBOOK_TAG = "[Playbooks]"


def _inject_playbooks():
    """Surface the fixed rules fresh each turn — unlike _recall, playbooks
    apply ALWAYS. Exactly ONE block, /reset clears it too."""
    _history[:] = [m for m in _history
                   if not (m.get("role") == "system"
                           and str(m.get("content", "")).startswith(PLAYBOOK_TAG))]
    try:
        pbs = json.loads(_mgrclient._mgr_get(_mgrclient._manager_base(), "/api/playbooks", timeout=6)).get("playbooks", [])
    except Exception:
        pbs = []
    if pbs:
        block = (PLAYBOOK_TAG + " Your fixed rules — ALWAYS follow:\n"
                 + "\n".join(f"- {p.get('text','')}" for p in pbs))
        # Appended, not inserted at the top: next to the question the rules
        # are followed; at the top gemini-flash kept saying "12:31 Uhr"
        # against a rule that forbids the "Uhr" (2026-09-08).
        _history.append({"role": "system", "content": block})


# --- prompt templates: /name -> prompt maintained in the manager ------------
# Recurring assignments as a command (pi.dev idea "prompt templates").
# Expansion happens HERE in the agent — so it works in web, app and
# Signal alike. "/daily please keep it short" -> template text + " please keep it short".
_BUILTIN_SLASH = ("/reset", "/fresh", "/reasoning", "/goal", "/model", "/steps",
                  "/aside", "/branch", "/back", "/tools")
_prompts_cache = {"ts": 0.0, "map": {}}


def _prompt_templates():
    if time.time() - _prompts_cache["ts"] > 30:
        try:
            lst = json.loads(_mgrclient._mgr_get(_mgrclient._manager_base(), "/api/prompts", timeout=6)).get("prompts", [])
            _prompts_cache["map"] = {p["name"]: p.get("text", "") for p in lst if p.get("name")}
        except Exception:
            pass                       # keep the old cache
        _prompts_cache["ts"] = time.time()
    return _prompts_cache["map"]


def _expand_prompt(message):
    m = message.strip()
    if not m.startswith("/") or m.startswith(_BUILTIN_SLASH):
        return message
    name, _, rest = m[1:].partition(" ")
    tpl = _prompt_templates().get(name)
    if not tpl:
        return message
    return tpl + ((" " + rest.strip()) if rest.strip() else "")


MISSION_TAG = "[Missions]"


NOW_TAG = "[Now]"
MEMINDEX_TAG = "[MemoryIndex]"
MEMORY_DIR = os.environ.get("MEMORY_DIR", "")


def _inject_memory_index():
    """The head of /memory/MEMORY.md every turn: what notes exist, where the
    timeline is — so the agent greps the folder instead of guessing. One
    block, refreshed each turn; absent when there is no memory folder."""
    _history[:] = [m for m in _history
                   if not (m.get("role") == "system"
                           and str(m.get("content", "")).startswith(MEMINDEX_TAG))]
    if not MEMORY_DIR:
        return
    try:
        with open(os.path.join(MEMORY_DIR, "MEMORY.md"), encoding="utf-8") as fh:
            head = "\n".join(fh.read().split("\n")[:60]).strip()
    except OSError:
        return
    if head:
        _history.append({"role": "system", "content":
                         MEMINDEX_TAG + f" Your long-term memory is the folder {MEMORY_DIR} "
                         "(notes/*.md, timeline/*.md; grep it, read the note you need, "
                         "write or edit notes as Markdown with [[slug]] links). Index:\n" + head})


def _now_line():
    """Current date and time in the instance's timezone (TZ from config.env,
    set by the manager from the host). The model had no clock at all: asked
    about 'this week' it fetched the date through a Home-Assistant tool and
    gave up when that call failed (2026-09-07)."""
    tz = os.environ.get("TZ") or "UTC"
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo(tz))
    except Exception:
        now = datetime.datetime.now().astimezone()
        tz = str(now.tzinfo)
    off = now.strftime("%z")
    off = off[:3] + ":" + off[3:] if len(off) == 5 else off
    return (f"{NOW_TAG} {now.strftime('%A, %Y-%m-%d %H:%M')} {now.tzname()} ({tz}, UTC{off}). "
            "This is the current time for the message below — earlier times stated "
            "in this conversation are outdated. Use it for 'today', 'this week', "
            "dates and times; no tool call needed. Tool results may carry UTC "
            "timestamps (ISO …Z): convert them to this zone before you state a time. "
            f"Datetimes you pass TO tools: ISO 8601 with this offset, e.g. "
            f"{now.strftime('%Y-%m-%d')}T18:30:00{off}.")


def _inject_now():
    """Exactly ONE [Now] system line per turn, refreshed every turn — placed
    LAST, right before the new user message. At the top of the context
    gemini-flash kept answering with a time from earlier turns (18:15 asked,
    '16:37' said); next to the question it is read."""
    _history[:] = [m for m in _history
                   if not (m.get("role") == "system"
                           and str(m.get("content", "")).startswith(NOW_TAG))]
    _history.append({"role": "system", "content": _now_line()})


def _inject_missions():
    """Surface active missions compactly each turn — this way the work state
    survives /reset and restart. For every agent: the manager returns only the
    missions this instance owns. Exactly ONE block, /reset clears it too."""
    _history[:] = [m for m in _history
                   if not (m.get("role") == "system"
                           and str(m.get("content", "")).startswith(MISSION_TAG))]
    try:
        ms = json.loads(_mgrclient._mgr_get(_mgrclient._manager_base(), "/api/missions", timeout=6)).get("missions", [])
    except Exception:
        ms = []
    lines = []
    for m in ms:
        if m.get("status") != "active":
            continue
        cur = next((st for st in m.get("steps", []) if st.get("status") == "doing"),
                   None) or next((st for st in m.get("steps", []) if st.get("status") == "open"), None)
        done = sum(1 for st in m.get("steps", []) if st.get("status") == "done")
        lines.append(f"- {m['id']}: {m['goal'][:100]} ({done}/{len(m.get('steps', []))} steps) — "
                     + (f"currently step {cur['n']}: {cur['text'][:80]} [{cur['status']}]"
                        + (f" @{cur['target']}" if cur.get("target") else "")
                        if cur else "all steps done -> mission_finish!"))
    if lines:
        _history.append({"role": "system", "content":
                            MISSION_TAG + " Your ongoing missions (progress lives in the "
                            "manager, use mission_update/mission_finish):\n" + "\n".join(lines)})


# Upper bound for the conversation _history. Without it the context of a
# long-running process (orchestrator: heartbeat + app chats share ONE _history)
# grows unbounded, and every call sends everything again. Trimming happens only
# BETWEEN turns (here, before the new user message) — never mid tool cycle,
# otherwise a tool result dangles without its tool_calls (API error).
CTX_MAX_MSGS = int(os.environ.get("CTX_MAX_MSGS", "20"))


def _trim_history():
    """On overflow, SUMMARIZE the older messages instead of discarding
    them (summarizing conversation manager). _history[0] (system) is
    pinned; the last CTX_PRESERVE_RECENT conversation messages stay
    verbatim; everything before is condensed into a [Summary] system block
    (an existing summary is folded in). Transient blocks
    (playbooks/memory) are discarded here — _inject/_recall set them
    up again right away. Only call BETWEEN turns, never in the tool cycle."""
    if len(_history) <= CTX_MAX_MSGS:
        return
    if _branch_depth() > 0:
        return          # open side branch: do not trim, the marker must stay
    head = _history[0]
    prior, convo = "", []
    for m in _history[1:]:
        if m.get("role") == "system":
            c = str(m.get("content", ""))
            if c.startswith(SUMMARY_TAG):
                prior = c[len(SUMMARY_TAG):].strip()
            continue    # playbook/recall/summary: do not treat as conversation
        convo.append(m)

    def _boundary_keep(msgs, n):
        """The last n messages, but starting at a user boundary, so that
        no tool result is orphaned from its assistant/tool_calls."""
        k = msgs[-n:] if n < len(msgs) else msgs[:]
        while k and k[0].get("role") != "user":
            k.pop(0)
        return k

    def _prefix(sm):
        return [{"role": "system", "content": SUMMARY_TAG + " " + sm}] if sm else []

    if not CTX_SUMMARY or len(convo) <= CTX_PRESERVE_RECENT:
        # summarizing off/too little -> old behavior, but keep the summary.
        _history[:] = [head] + _prefix(prior) + _boundary_keep(convo, CTX_MAX_MSGS - 1)
        return
    recent = _boundary_keep(convo, CTX_PRESERVE_RECENT)
    to_sum = convo[:len(convo) - len(recent)]
    new_summary = _summarize(to_sum, prior) if to_sum else prior
    if not new_summary:
        # summarizer unavailable -> do not risk losing more context: discard.
        _history[:] = [head] + _prefix(prior) + recent
        return
    _history[:] = [head] + _prefix(new_summary) + recent



# --- branches (tree chat): side question in inherited context, clean return --
# /aside opens an aside: a marker remembers the point. /back closes
# the innermost branch: everything after the marker is condensed into ONE sidenote
# (or discarded without a trace with "drop") — the main topic stays unpolluted
# but informed. Nesting is possible (a stack via markers in the history).
BRANCH_MARK = "[Branch]"
NOTE_TAG = "[Sidenote]"


def _branch_depth():
    return sum(1 for m in _history
               if m.get("role") == "system"
               and str(m.get("content", "")).startswith(BRANCH_MARK))


def _branch_open(cmd):
    # /aside is the name, /branch the silent alias (muscle memory, old
    # playbooks — and Claude Code owns "/branch" for git worktrees, which made
    # the old name collide in people's heads).
    word = "/aside" if cmd.startswith("/aside") else "/branch"
    thema = cmd[len(word):].strip()
    _history.append({"role": "system", "content":
                     BRANCH_MARK + (f" Aside: {thema}" if thema else " Aside") +
                     " — the user asks a question aside from the main topic."})
    return f"⑂ Aside opened (depth {_branch_depth()})." + \
        (f" Topic: {thema}" if thema else "")


def _branch_close(cmd):
    drop = cmd[len("/back"):].strip().lower() in ("drop", "verwerfen")
    idx = None
    for i in range(len(_history) - 1, 0, -1):
        m = _history[i]
        if m.get("role") == "system" and str(m.get("content", "")).startswith(BRANCH_MARK):
            idx = i
            break
    if idx is None:
        return "No open side branch."
    segment = _history[idx + 1:]
    note = ""
    if not drop and segment:
        try:
            lines = []
            for m in segment:
                c = _msg_text(m)
                if m.get("role") in ("user", "assistant") and c:
                    lines.append(("User: " if m["role"] == "user" else "Agent: ") + c[:300])
            r = _llm.or_chat([{"role": "system", "content":
                          "Summarize this side branch of a conversation in ONE line (max 140 "
                          "characters): the core question and the outcome. Just the line."},
                         {"role": "user", "content": "\n".join(lines)[:6000]}], [])
            note = (r.get("content") or "").strip().splitlines()[0][:160]
        except Exception:
            note = ""
    del _history[idx:]
    if note:
        _history.append({"role": "system", "content": f"{NOTE_TAG} Side branch resolved: {note}"})
    left = _branch_depth()
    return ("↩ Back in the " + ("main topic" if left == 0 else f"branch depth {left}") +
            ("." if drop or not note else f" — sidenote: {note}"))


# Wall-clock budget of the current turn (epoch seconds, 0 = none). The manager
# sets it per task from its own timeout: a task that ran 12 steps of slow web
# fetches once needed more than the manager's 10 minutes, the manager gave up,
# the agent finished into the void and the result was lost. With a deadline
# the loop stops fetching in time and answers with what it has.
_deadline = [0.0]
