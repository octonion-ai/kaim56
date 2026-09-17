#!/usr/bin/env python3
# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""OpenRouter agent with tool-calling — model-agnostic, runs inside the microVM.

Tools: bash, read_file, write_file, list_dir, http_fetch  + optional MCP servers
(stdio), fetched from the manager at runtime. Transports: signal | web (via TRANSPORT). Stdlib only.
"""
import json
import os
import re
import threading
import time
import uuid

# ---- package modules ----
from . import context as _context
from . import tools_manager as _tools_manager
from . import offload as _offload
from . import learn as _learn
from . import llm as _llm
from . import mcp as _mcp
from . import tools_local as _tools_local
from . import observe as _observe
from . import mgrclient as _mgrclient
from . import config as _config

BUILTIN = {
    "bash": (_tools_local.t_bash, "Run a shell command in the workspace",
             {"command": {"type": "string", "description": "command"}}, ["command"]),
    "read_file": (_tools_local.t_read_file, "Read a file",
                  {"path": {"type": "string"}}, ["path"]),
    "write_file": (_tools_local.t_write_file, "Write a file",
                   {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    "write_xlsx": (_tools_local.t_write_xlsx, "Write a spreadsheet (.xlsx) into the workspace — for lists and tables the user "
                   "will filter or sort (jobs, results, inventories). rows: first row = header.",
                   {"path": {"type": "string", "description": "file name, e.g. jobs.xlsx"},
                    "rows": {"type": "array", "items": {}, "description": "list of rows (arrays; first = header) or of objects (keys = header)"},
                    "sheet": {"type": "string", "description": "sheet name (optional)"}}, ["path", "rows"]),
    "write_docx": (_tools_local.t_write_docx, "Write a Word document (.docx) into the workspace from Markdown "
                   "(# headings, - bullets, paragraphs, **bold**) — for letters, reports, CVs.",
                   {"path": {"type": "string", "description": "file name, e.g. anschreiben.docx"},
                    "markdown": {"type": "string"},
                    "title": {"type": "string", "description": "document title (optional)"}}, ["path", "markdown"]),
    "list_dir": (_tools_local.t_list_dir, "List a directory",
                 {"path": {"type": "string"}}, []),
    "http_fetch": (_tools_local.t_http_fetch,
                   "Fetch a URL. HTML comes back as readable TEXT with link targets "
                   "in brackets — follow them with another fetch. raw=true for the "
                   "unconverted body.",
                   {"url": {"type": "string"},
                    "method": {"type": "string", "description": "GET (default) or POST"},
                    "raw": {"type": "boolean", "description": "true = raw HTML/body"}},
                   ["url"]),
    "read_pdf": (_tools_local.t_read_pdf, "Extract text from a PDF — path is a workspace file OR an http(s) URL; pages optional as a range (e.g. '1-5').",
                 {"path": {"type": "string", "description": "file in the workspace or http(s) URL"},
                  "pages": {"type": "string", "description": "optional page range, e.g. '1-5'"}}, ["path"]),
    "web_search": (_tools_local.t_web_search,
                   "Web search (Brave Search API via the manager; DuckDuckGo/Bing "
                   "as fallback). Returns title + URL + snippet.",
                   {"query": {"type": "string", "description": "search terms"},
                    "count": {"type": "integer", "description": "results (1-10, default 5)"}},
                   ["query"]),
    "spawn_subagent": (_tools_manager.t_spawn_subagent,
                       "Delegate a self-contained subtask to a fresh ephemeral VM and wait for its answer "
                       "(the manager creates and deletes the VM). Optionally pick the subagent's model — "
                       "e.g. a cheap/fast one for grunt work or a strong one for hard reasoning.",
                       {"task": {"type": "string", "description": "task for the subagent (self-contained: it has no memory of this chat)"},
                        "model": {"type": "string", "description": "optional OpenRouter model id for the subagent, e.g. google/gemini-2.5-flash"},
                        "tools": {"type": "string", "description": "optional: comma-separated subset of your own tools the subagent may use (narrower cage), e.g. 'bash,read_file,write_file'"},
                        "egress": {"type": "string", "description": "optional: comma-separated hosts the subagent may reach, or 'none' for no network at all"},
                        "skill": {"type": "string", "description": "optional: a skill from list_skills baked into the subagent's system prompt; without `tools` it then gets only the file/web tools"},
                        "persona": {"type": "string", "description": "optional: a named agent persona (e.g. code-reviewer, security-reviewer) baked into the subagent's system prompt; its recommended tools/model apply unless you override them"}}, ["task"]),
    "create_task": (_tools_manager.t_create_task,
                    "Queue a task — IMPORTANT: choose target by capability. "
                    "If the task needs a specific MCP/token (e.g. Home Assistant), "
                    "use the matching instance as target (e.g. 'hass'). For general/"
                    "isolated work use 'ephemeral' (fresh VM, deleted afterwards). schedule "
                    "optional ('every 2h','daily 08:00','hourly'). wait=true waits for the "
                    "result, otherwise it runs in the background and appears in the chat.",
                    {"task": {"type": "string", "description": "what should be done"},
                     "target": {"type": "string", "description": "instance name (capable) or 'ephemeral'"},
                     "schedule": {"type": "string", "description": "optional: every Nm|Nh|Nd, daily HH:MM, hourly"},
                     "wait": {"type": "boolean", "description": "wait for the result (default false)"},
                     "model": {"type": "string", "description": "optional OpenRouter model for an ephemeral target"}}, ["task"]),
    "mission_start": (_tools_manager.t_mission_start,
                      "Create a multi-stage assignment as a mission (goal + steps). For anything "
                      "that needs several tasks/days — the progress survives restarts.",
                      {"goal": {"type": "string", "description": "goal of the mission"},
                       "steps": {"type": "array", "items": {"type": "string"},
                                 "description": "planned steps in order"}},
                      ["goal", "steps"]),
    "missions": (_tools_manager.t_missions, "List open missions with steps/status.", {}, []),
    "mission_update": (_tools_manager.t_mission_update,
                       "Advance a mission step: set status (doing/done/failed), "
                       "record result + task_id AND the target instance of the kicked-off "
                       "task, add_step appends a step.",
                       {"id": {"type": "string", "description": "mission ID"},
                        "step": {"type": "integer", "description": "step number"},
                        "status": {"type": "string", "description": "open|doing|done|failed"},
                        "result": {"type": "string", "description": "short result"},
                        "task_id": {"type": "string", "description": "ID of the create_task task"},
                        "add_step": {"type": "string", "description": "append a new step"},
                        "note": {"type": "string", "description": "log note only"},
                        "target": {"type": "string",
                                   "description": "instance the step was delegated to "
                                                  "(create_task target)"}}, ["id"]),
    "mission_finish": (_tools_manager.t_mission_finish,
                       "Finish a mission; failed=true on failure. Provide a short conclusion.",
                       {"id": {"type": "string"}, "summary": {"type": "string"},
                        "failed": {"type": "boolean"}}, ["id", "summary"]),
    "oracle": (_tools_manager.t_oracle,
               "Second opinion BEFORE a risky/irreversible action: challenges your "
               "assumptions, never acts itself. plan = what you intend and why; kontext = "
               "relevant facts (IDs, wordings, user assignment). On 'OBJECTION' do not "
               "act, but resolve it or ask back.",
               {"plan": {"type": "string", "description": "planned action + reasoning"},
                "kontext": {"type": "string", "description": "facts: IDs, wordings, assignment"}},
               ["plan"]),
    "ha_control": (_tools_manager.t_ha_control,
                   "Turn a Home Assistant device OR whole room on/off by the SPOKEN name "
                   "(manager matches real entities/areas server-side and auto-learns the "
                   "alias on a fuzzy hit). Prefer this over raw HA intents for voice control: "
                   "pass the heard target verbatim and action on/off. Handles rooms too "
                   "('Licht im Gartenhaus').",
                   {"spoken": {"type": "string", "description": "the spoken target, e.g. 'Gartenhaus denke rechts' or 'Licht im Gartenhaus'"},
                    "action": {"type": "string", "description": "'on' or 'off'"}},
                   ["spoken", "action"]),
    "ha_learn_alias": (_tools_manager.t_ha_learn_alias,
                       "Teach Home Assistant a spoken-name alias for an entity so the same "
                       "misheard wording matches natively next time (STT hears 'Decke' as "
                       "'denke'). Call it after recovering from a failed HA intent, with the "
                       "words you originally heard and the real entity id.",
                       {"spoken": {"type": "string", "description": "the spoken/misheard name, e.g. 'Gartenhaus denke rechts'"},
                        "entity": {"type": "string", "description": "real entity id, e.g. 'light.gartenhaus_decke_rechts'"}},
                       ["spoken", "entity"]),
    "notify": (_tools_manager.t_notify,
               "Push notification to the user's devices (app system notification + "
               "web-manager bell). For important events/results when they are not in the "
               "chat. Unlike send_signal this is the app/web channel, does not ring "
               "in Signal.",
               {"title": {"type": "string", "description": "short title"},
                "message": {"type": "string", "description": "text of the notification"}},
               ["title"]),
    "send_signal": (_tools_manager.t_send_signal,
                    "Send the user a Signal message — for results, findings "
                    "or questions when they are not currently in the chat. Do NOT use for the "
                    "normal reply in an ongoing conversation (that arrives anyway) "
                    "and not repeatedly unprompted: a message rings on a "
                    "phone. Recipients only from the allowed list; leaving 'to' empty "
                    "means: to the default recipient.",
                    {"text": {"type": "string", "description": "message text"},
                     "to": {"type": "string", "description": "optional: number in the format +49…"}},
                    ["text"]),
    "read_inbox": (_tools_manager.t_read_inbox,
                  "Read new user messages (Signal/app/web) since the last run — "
                  "the orchestrator's inbox. Each message comes only once (watermark); "
                  "peek=true to preview without consuming.",
                  {"peek": {"type": "boolean", "description": "only look, do not consume"}}, []),
    "list_agents": (_tools_manager.t_list_agents,
                    "List available agent instances + capabilities (model/MCP). "
                    "For routing: choose the create_task target by capability.",
                    {}, []),
    "recall_tasks": (_tools_manager.t_recall_tasks,
                     "Query previously executed tasks + results (long-term memory). "
                     "Without query the most recent, with query search specifically. Use BEFORE create_task "
                     "to check whether something is already done/scheduled (no duplicates).",
                     {"query": {"type": "string", "description": "search term (empty = most recent)"},
                      "limit": {"type": "integer", "description": "max hits (default 10)"}}, []),
    "list_tasks": (_tools_manager.t_list_tasks,
                   "List RUNNING/scheduled tasks with IDs — for targeted deletion. "
                   "(recall_tasks, by contrast, is the history of completed runs.)", {}, []),
    "delete_task": (_tools_manager.t_delete_task,
                    "Delete a running/scheduled task by ID. Get the ID first with "
                    "list_tasks. Final.",
                    {"id": {"type": "string", "description": "task ID from list_tasks"}}, ["id"]),
    "edit_task": (_tools_manager.t_edit_task,
                  "Change the message and/or schedule of a task (ID from list_tasks). "
                  "schedule e.g. 'every 2h', 'daily 08:00', 'hourly'; empty = one-off.",
                  {"id": {"type": "string", "description": "task ID from list_tasks"},
                   "message": {"type": "string", "description": "new text (empty = unchanged)"},
                   "schedule": {"type": "string", "description": "new schedule (empty = one-off/unchanged)"}},
                  ["id"]),
    "search_sessions": (_tools_manager.t_search_sessions,
                        "Full-text search over earlier chats and task results (exact words, "
                        "newest and best matches first). Use memory_recall for meaning, "
                        "this for names, numbers, URLs you remember seeing.",
                        {"query": {"type": "string", "description": "words to look for"},
                         "instance": {"type": "string", "description": "optional: another instance (orchestrator only)"}},
                        ["query"]),
    "propose_skill": (_tools_manager.t_propose_skill,
                      "Propose a reusable procedure as a skill for the catalog (after a "
                      "non-trivial task that worked, or after the user corrected your approach). "
                      "The operator approves it in the Skills tab.",
                      {"name": {"type": "string", "description": "kebab-case name"},
                       "description": {"type": "string", "description": "one line: what it is for"},
                       "content": {"type": "string", "description": "Markdown: purpose, when to use, exact steps and tools, pitfalls; no secrets"}},
                      ["name", "description", "content"]),
    "list_skills": (_tools_manager.t_list_skills,
                    "List available expert skills. Without arguments: names only. "
                    "query='…' searches names AND descriptions. Before specialized "
                    "tasks, check whether a matching skill exists.",
                    {"query": {"type": "string",
                               "description": "optional: filter, e.g. 'docker' or 'security'"}},
                    []),

    "load_skill": (_tools_manager.t_load_skill, "Load an expert skill (knowledge document) into the context and follow it.",
                   {"name": {"type": "string", "description": "skill name from list_skills"}}, ["name"]),
    "memory_store": (_tools_manager.t_memory_store, "Store a value permanently (survives restart/instance deletion).",
                     {"key": {"type": "string"}, "value": {"type": "string"}}, ["key", "value"]),
    "memory_recall": (_tools_manager.t_memory_recall, "Retrieve a stored value; without key all entries.",
                      {"key": {"type": "string"}}, []),
    "memory_reflect": (_tools_manager.t_memory_reflect,
                       "Ask the long-term memory a question and get a reasoned answer over everything "
                       "remembered (past conversations, notes). Use for 'what do we know about…', "
                       "'what did the user say about…', preferences and history.",
                       {"question": {"type": "string"}}, ["question"]),
    "playbook_add": (_tools_manager.t_playbook_add,
                     "Record a permanent rule/procedure — applies ALWAYS from now on. "
                     "Use this when the user tells you HOW something is to be done, states a "
                     "lasting preference or corrects you.",
                     {"rule": {"type": "string", "description": "the rule as a short, concrete sentence"}}, ["rule"]),
    "playbooks": (_tools_manager.t_playbooks, "Show all fixed rules (playbooks) with IDs.", {}, []),
    "playbook_forget": (_tools_manager.t_playbook_forget, "Remove a rule by ID (ID from playbooks).",
                        {"id": {"type": "string", "description": "playbook ID"}}, ["id"]),
    "remote_ls": (_tools_manager.t_remote_ls,
                  "List the folder the user has shared (lives on THEIR machine, "
                  "connected via P2P). Paths are relative to the root of the share.",
                  {"path": {"type": "string", "description": "relative, default '.'"}}, []),
    "remote_read": (_tools_manager.t_remote_read,
                    "Read a file from the user's shared folder (path relative to the share).",
                    {"path": {"type": "string"}}, ["path"]),
    "remote_write": (_tools_manager.t_remote_write,
                     "Write a file to the user's shared folder — CREATES and "
                     "OVERWRITES, missing subfolders are created automatically. Write access "
                     "is explicitly allowed: when the user wants to put, save or "
                     "change something there, CALL THIS TOOL instead of claiming you cannot "
                     "write. Only if it returns an error is it not possible.",
                     {"path": {"type": "string", "description": "relative to the share, e.g. 'note.txt'"},
                      "content": {"type": "string", "description": "complete new file content"}},
                     ["path", "content"]),
    "remote_delete": (_tools_manager.t_remote_delete,
                      "Delete a file or folder in the user's shared folder. "
                      "Irreversible — there is no trash. Only delete when the user "
                      "requests it, and ask first when in doubt. A non-empty folder "
                      "fails on purpose; set recursive=true for that.",
                      {"path": {"type": "string", "description": "relative to the share"},
                       "recursive": {"type": "boolean",
                                     "description": "delete the folder including its contents (default false)"}},
                      ["path"]),
    "list_secrets": (_tools_manager.t_list_secrets, "Show the secret names released for this agent (no values).",
                     {}, []),
    "get_secret": (_tools_manager.t_get_secret, "Fetch a released secret (e.g. API key/token) only when needed. Never output values in replies/logs.",
                   {"name": {"type": "string"}}, ["name"]),
}


# Optional per-instance tool allowlist (AGENT_TOOLS, comma-separated). Empty =
# all. Filters both the schema reported to the model AND the
# execution — otherwise a model could call a disabled tool anyway.
# MCP tools are unaffected by this (those are controlled by MCP_SERVERS/policy).
_TOOL_ALLOW = {t.strip() for t in os.environ.get("AGENT_TOOLS", "").split(",") if t.strip()}


# Task administration only where the manager has set TASK_ADMIN (orchestrator).
# The MISSION tools are deliberately NOT in here: every agent may plan its own
# mission and delegate the steps to capable instances (create_task target). Each
# agent only ever sees and writes its own missions — the manager keys them by
# the calling instance.
_TASK_ADMIN_TOOLS = {"list_tasks", "delete_task", "edit_task"}


def tool_enabled(name):
    if name == "offload_read":
        return True   # system helper: must always be available, otherwise a reference dangles
    if name == "spawn_subagent" and os.environ.get("NO_SPAWN"):
        return False
    if name in _TASK_ADMIN_TOOLS and not os.environ.get("TASK_ADMIN"):
        return False
    return (not _TOOL_ALLOW) or name in _TOOL_ALLOW


def builtin_schema():
    out = []
    for name, (_fn, desc, props, req) in BUILTIN.items():
        if not tool_enabled(name):
            continue
        out.append({"type": "function", "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req}}})
    return out


def _resolve_tool_name(name):
    """Models drop the MCP prefix now and then — 'mrmusic_power' for
    'mrmusic__mrmusic_power' (gemini-2.5-flash, 2026-09-07, 'unknown tool'
    twice while the user waited for the radio). A bare name that matches
    exactly ONE registered MCP tool is accepted; ambiguity stays unknown."""
    if name in BUILTIN or name in _mcp._mcp_tools:
        return name
    hits = [fq for fq, (_srv, tool) in _mcp._mcp_tools.items()
            if tool == name or fq.endswith("__" + name)]
    if len(hits) == 1:
        _config.log(f"tool name '{name}' resolved to '{hits[0]}'")
        return hits[0]
    return name


def _tools_report():
    """'/tools' — the registry as the model sees it, without a model call.
    Built-in tools that are enabled here, then every MCP tool with its full
    name. A smoke test after a rootfs rebuild reads exactly this."""
    builtin = sorted(n for n in BUILTIN if tool_enabled(n))
    mcp = sorted(_mcp._mcp_tools)
    out = [f"built-in ({len(builtin)}): " + ", ".join(builtin)]
    if mcp:
        out.append(f"mcp ({len(mcp)}): " + ", ".join(mcp))
    else:
        out.append("mcp (0): none registered")
    return "\n".join(out)


def exec_tool(name, args):
    name = _resolve_tool_name(name)
    t0 = time.monotonic()

    def _audit(**kw):        # every exit books the call with its duration
        _observe.audit(name, args, ms=int((time.monotonic() - t0) * 1000), **kw)
    # Hook/intervention: denylist + optional HITL approval BEFORE execution.
    allow, reason = _hook_before_tool(name, args)
    if not allow:
        _audit(ok=False)
        return f"Tool '{name}' not executed: {reason}"
    try:
        if name in BUILTIN:
            if not tool_enabled(name):
                _audit(ok=False, err="tool not enabled")
                return f"Tool '{name}' is not enabled for this instance."
            out = str(BUILTIN[name][0](**args))
        elif name in _mcp._mcp_tools:
            srv, tool = _mcp._mcp_tools[name]
            out = str(_mcp._mcp[srv].call(tool, args))
        else:
            _audit(ok=False, err="unknown tool")
            return f"unknown tool: {name}"
    except Exception as e:
        _audit(ok=False, err=repr(e))
        return f"Tool error ({name}): {e!r}"
    failed = _learn._looks_failed(out)
    _audit(ok=not failed, err=out[:300] if failed else "", result="" if failed else out[:200])
    return _offload._finalize_output(name, out)


BUILTIN["offload_read"] = (
    _offload.t_offload_read,
    "Re-read a previously offloaded, truncated tool output in chunks "
    "(the offload reference names id and offset).",
    {"id": {"type": "string", "description": "offload id from the reference"},
     "offset": {"type": "integer", "description": "start position (characters)"},
     "length": {"type": "integer", "description": "max characters (default 8000)"}},
    ["id"])


# --- 4a) goal loop ---------------------------------------------------------
GOAL_MAX_ATTEMPTS = int(os.environ.get("GOAL_MAX_ATTEMPTS", "3"))
_goal = (os.environ.get("AGENT_GOAL", "").strip() or None)
JUDGE_PROMPT = (
    "You are a strict reviewer. Check whether the ANSWER meets the GOAL for the "
    "QUESTION. Answer EXCLUSIVELY with JSON, no other text: "
    '{"meets": true|false, "feedback": "concise reasoning, what is still missing"}.')


def _set_goal(cmd):
    global _goal
    rest = cmd[len("/goal"):].strip()
    if rest in ("", "show", "status"):
        return f"\U0001f3af Goal: {_goal}" if _goal else \
            "No goal set. /goal <criterion> sets one, /goal off removes it."
    if rest in ("off", "clear", "none", "aus"):
        _goal = None
        return "\U0001f3af Goal removed."
    _goal = rest
    return f"\U0001f3af Goal set (max {GOAL_MAX_ATTEMPTS} attempts): {_goal}"


def _judge(goal, question, answer):
    """(meets, feedback). Judge broken/unparseable -> let it pass (True)."""
    try:
        m = _llm.or_chat([{"role": "system", "content": JUDGE_PROMPT},
                     {"role": "user", "content": f"GOAL:\n{goal}\n\nQUESTION:\n{question}\n\nANSWER:\n{answer}"}], [])
        raw = (m.get("content") or "").strip()
        d = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        return bool(d.get("meets")), str(d.get("feedback", ""))[:500]
    except Exception:
        return True, ""


def _run_goal(hist, question):
    """Produce an answer and check it against _goal; on non-fulfillment improve it
    with the judge's critique, up to max GOAL_MAX_ATTEMPTS."""
    answer = _tool_loop(hist)
    for _ in range(GOAL_MAX_ATTEMPTS - 1):
        meets, fb = _judge(_goal, question, answer)
        if meets:
            break
        hist.append({"role": "user", "content":
                     f"Your last answer does not yet meet the goal: {_goal}. "
                     f"Critique: {fb}. Improve the answer accordingly."})
        answer = _tool_loop(hist)
    return answer


# --- 4b) tool hook: hard denylist + optional HITL approval ------------------
HITL = os.environ.get("HITL", "") not in ("", "0", "false", "False")
HITL_TOOLS = set(t for t in os.environ.get(
    "HITL_TOOLS", "bash,remote_delete,remote_write,delete_task,edit_task").split(",") if t)
HITL_TIMEOUT = int(os.environ.get("HITL_TIMEOUT", "120"))
# Always active, independent of HITL: obviously destructive bash patterns.
_DENY_PATTERNS = ("rm -rf /", ":(){:|:&};:", "mkfs", "dd if=", "> /dev/sd", "chmod -R 000")


def _request_approval(name, args):
    """Request an approval from the manager (which asks the user via Signal) and
    poll for it. If the manager cannot (old version/no Signal) -> do not
    block (True). Timeout/rejection -> False."""
    try:
        d = json.loads(_mgrclient._mgr(_mgrclient._manager_base(), "/api/hitl",
                            {"tool": name, "target": _observe._audit_target(name, args)}, timeout=8))
        hid = d.get("id")
        if not hid:
            return True
    except Exception:
        return True
    deadline = time.time() + HITL_TIMEOUT
    while time.time() < deadline:
        time.sleep(2)
        try:
            st = json.loads(_mgrclient._mgr_get(_mgrclient._manager_base(), f"/api/hitl/{hid}", timeout=6)).get("status")
        except Exception:
            continue
        if st == "approved":
            return True
        if st == "denied":
            return False
    return False


def _hook_before_tool(name, args):
    """(allow, reason). Denylist first, then optional HITL approval."""
    if name == "bash":
        cmd = str(args.get("command", ""))
        for pat in _DENY_PATTERNS:
            if pat in cmd:
                return False, f"blocked by security rule ({pat})"
    if HITL and name in HITL_TOOLS:
        if not _request_approval(name, args):
            return False, "not approved by the user (or timed out)"
    return True, ""


TOOLS = []
# --- steering: interrupt the running agent -----------------------------------
# While a turn is running (tool loop), the user can push in additional messages
# (run_agent: POST /api/steer). They are fed in between two tool
# steps as a user message — the agent changes course instead of
# stubbornly running to the end.
_steer_lock = threading.Lock()
_steer_q = []
_busy = [False]
# Auto-reset: a context that idled for AUTO_RESET_MIN minutes starts over at
# the next turn (0 = never). For a voice instance every "radio on" otherwise
# pays for the whole day's history on each of its two model calls.
AUTO_RESET_MIN = int(os.environ.get("AUTO_RESET_MIN", "0") or 0)
_last_turn = [0.0]


def _auto_reset():
    """Called at the start of a turn, before the injections: drops the
    conversation if the last turn is older than AUTO_RESET_MIN minutes.
    Returns True when it did (the turn then starts on a fresh context)."""
    now = time.time()
    last, _last_turn[0] = _last_turn[0], now
    if AUTO_RESET_MIN <= 0 or not last or now - last < AUTO_RESET_MIN * 60 or len(_context._history) <= 1:
        return False
    del _context._history[1:]
    _config.log(f"auto-reset: context idle for {int((now - last) // 60)} min (> {AUTO_RESET_MIN}), starting fresh")
    return True


def steer_push(msg):
    """(accepted?) True if a turn is running and the message is fed in;
    False -> the caller should send it as a normal message."""
    with _steer_lock:
        if not _busy[0]:
            return False
        _steer_q.append(str(msg)[:2000])
        return True


def _drain_steer(hist, on_token=None):
    with _steer_lock:
        msgs, _steer_q[:] = _steer_q[:], []
    for m in msgs:
        hist.append({"role": "user", "content":
                     "[Steering — just pushed in by the user, takes priority] " + m})
        if on_token:
            on_token(f"\n\u21aa {m}\n")
    return bool(msgs)


DEADLINE_MARGIN = 45           # seconds before the deadline reserved for the final answer
_DEADLINE_NOTE = ("[TimeBudget] The time budget for this turn is exhausted. Answer NOW with "
                  "what you have: a partial result is fine, say what is still missing. "
                  "No more tools.")


def _out_of_time():
    return bool(_context._deadline[0]) and time.time() > _context._deadline[0] - DEADLINE_MARGIN


def _tool_loop(hist):
    """Tool loop on an arbitrary message list. `hist` is either
    the persistent _history (conversation) or a throwaway list (heartbeat)."""
    for _ in _config._step_iter():
        _drain_steer(hist)
        if _out_of_time():
            hist.append({"role": "system", "content": _DEADLINE_NOTE})
            msg = _llm.or_chat(hist, [])                       # tools off: the final answer
            hist.append(msg)
            return (msg.get("content") or "(empty answer)") + "\n\n⏱️ (time budget exhausted — partial result)"
        msg = _llm.or_chat(hist, TOOLS)
        hist.append(msg)
        tcs = msg.get("tool_calls")
        if not tcs:
            # Did a steering message arrive during the answer? Then continue.
            if _drain_steer(hist):
                continue
            return msg.get("content") or "(empty answer)"
        for tc in tcs:
            fn = tc["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            out = "(time budget exhausted — not executed)" if _out_of_time() else exec_tool(fn["name"], args)
            _config.log("tool", fn["name"], "->", "(redacted)" if fn["name"] == "get_secret" else out[:80].replace("\n", " "))
            hist.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
    return "(max tool steps reached)"


_STEPS_PREFIX = re.compile(r"^/(?:steps|maxsteps)\s+(\d+|unlimited)\s+(\S.*)$", re.I | re.S)


def _turn_steps(message):
    """'/steps 40 <text>' (alias /maxSteps): the step cap for THIS turn only,
    (n, text); None when the message is not of that form. A task once carried
    '/maxSteps 100 …' as plain text — no such command, the model just read it."""
    m = _STEPS_PREFIX.match(message.strip())
    if not m:
        return None
    n = 0 if m.group(1).lower() == "unlimited" else max(1, int(m.group(1)))
    return n, m.group(2).strip()


def run(user_message, deadline=0.0, kind="chat", turn=None):
    """`turn`: the bridge names the turn up front so it can hand the id to the
    client in a response header — the client then fetches the trace."""
    _context._deadline[0] = float(deadline or 0)
    ts = _turn_steps(user_message)
    if ts:
        saved, _config.MAX_STEPS = _config.MAX_STEPS, ts[0]      # config's module global, per turn
        try:
            return run(ts[1], kind=kind, turn=turn)
        finally:
            _config.MAX_STEPS = saved
    _observe._turn_id[0] = turn or uuid.uuid4().hex[:8]
    user_message = _context._expand_prompt(user_message)
    if user_message.strip() == "/reset":
        del _context._history[1:]
        globals()["_goal"] = None      # a stale goal would drive the goal loop on every later turn
        return "🔄 Context reset."
    if user_message.startswith("/reasoning"):
        return _config._set_reasoning(user_message)
    if user_message.startswith("/goal"):
        return _set_goal(user_message)
    if user_message.strip() == "/tools":
        return _tools_report()
    if user_message.startswith("/model"):
        return _config._set_model(user_message)
    if user_message.startswith("/steps"):
        return _config._set_steps(user_message)
    if user_message.startswith(("/aside", "/branch")):
        return _context._branch_open(user_message)
    if user_message.startswith("/back"):
        return _context._branch_close(user_message)
    # /fresh: run statelessly in a throwaway context — the conversation
    # _history stays untouched (otherwise a heartbeat would wipe out a running
    # app chat, because both share the same _history). For the
    # orchestrator heartbeat: look, delegate, discard.
    if user_message.startswith("/fresh"):
        m = user_message[len("/fresh"):].strip()
        hist = [{"role": "system", "content": _config.SYSTEM},
                {"role": "system", "content": _context._now_line()},
                {"role": "user", "content": m}]
        _observe._trace_begin("fresh")
        out = "⚠️ (no answer)"
        try:
            out = _tool_loop(hist)
            return out
        finally:
            _observe._trace_end(_learn._outcome_of(out))
            _learn._maybe_learn(hist, m, _learn._outcome_of(out))
    _auto_reset()
    _context._trim_history()
    _context._inject_playbooks()
    _context._inject_missions()
    _context._inject_memory_index()
    _context._inject_now()
    _context._recall(user_message)
    _context._history.append({"role": "user", "content": user_message})
    _busy[0] = True
    _observe._trace_begin(kind)
    out = "⚠️ (no answer)"
    try:
        out = _run_goal(_context._history, user_message) if _goal else _tool_loop(_context._history)
        return out
    finally:
        _busy[0] = False
        _observe._trace_end(_learn._outcome_of(out))
        _learn._maybe_learn(_context._history, user_message, _learn._outcome_of(out))


def run_stream(user_message, on_token, image=None, deadline=0.0, kind="stream", turn=None):
    _context._deadline[0] = float(deadline or 0)
    _observe._turn_id[0] = turn or uuid.uuid4().hex[:8]
    """Like run(), but streams the answer tokens via on_token. Tool rounds
    produce no text; the final answer is streamed.
    image: optional base64 JPEG -> sent as vision content to OpenRouter."""
    user_message = _context._expand_prompt(user_message)
    if user_message.strip() == "/reset":
        del _context._history[1:]
        globals()["_goal"] = None      # a stale goal would drive the goal loop on every later turn
        on_token("🔄 Context reset.")
        return
    if user_message.startswith("/reasoning"):
        on_token(_config._set_reasoning(user_message))
        return
    if user_message.startswith("/goal"):
        on_token(_set_goal(user_message))
        return
    if user_message.strip() == "/tools":
        on_token(_tools_report())
        return
    if user_message.startswith("/model"):
        on_token(_config._set_model(user_message))
        return
    if user_message.startswith("/steps"):
        on_token(_config._set_steps(user_message))
        return
    if user_message.startswith(("/aside", "/branch")):
        on_token(_context._branch_open(user_message))
        return
    if user_message.startswith("/back"):
        on_token(_context._branch_close(user_message))
        return
    # /fresh: stateless as in run(), conversation untouched. Heartbeats
    # need no streaming — emit the answer once.
    if user_message.startswith("/fresh"):
        m = user_message[len("/fresh"):].strip()
        on_token(_tool_loop([{"role": "system", "content": _config.SYSTEM},
                             {"role": "user", "content": m}]))
        return
    _auto_reset()
    _context._trim_history()
    _context._inject_playbooks()
    _context._inject_missions()
    _context._inject_memory_index()
    _context._inject_now()
    _context._recall(user_message)
    if image:
        content = [
            {"type": "text", "text": user_message or "What is in the image?"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
        ]
    else:
        content = user_message
    _context._history.append({"role": "user", "content": content})
    _busy[0] = True
    _observe._trace_begin(kind)
    outcome = "error"
    try:
        if _goal:
            # With an active goal the answer is refined against the judge (not
            # streamed) and then emitted as a whole.
            ans = _run_goal(_context._history, user_message)
            on_token(ans)
            outcome = _learn._outcome_of(ans)
            return
        for _ in _config._step_iter():
            _drain_steer(_context._history, on_token)
            if _out_of_time():
                _context._history.append({"role": "system", "content": _DEADLINE_NOTE})
                _context._history.append(_llm.or_chat_stream(_context._history, [], on_token))
                on_token("\n\n⏱️ (time budget exhausted — partial result)")
                outcome = "deadline"
                return
            msg = _llm.or_chat_stream(_context._history, TOOLS, on_token)
            _context._history.append(msg)
            tcs = msg.get("tool_calls")
            if not tcs:
                if _drain_steer(_context._history, on_token):
                    continue
                outcome = _learn._outcome_of(msg.get("content"))
                return
            for tc in tcs:
                fn = tc["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                on_token(f"\n\U0001f527 {fn['name']} \u2026")
                _hb_stop = threading.Event()
                def _heartbeat(ev=_hb_stop):
                    while not ev.wait(_config.HEARTBEAT_SEC):
                        try:
                            on_token(" \u00b7")
                        except Exception:
                            return
                _hb = threading.Thread(target=_heartbeat, daemon=True)
                _hb.start()
                try:
                    out = exec_tool(fn["name"], args)
                finally:
                    _hb_stop.set()
                    _hb.join(timeout=1)
                on_token("\n")
                _config.log("tool", fn["name"], "->", "(redacted)" if fn["name"] == "get_secret" else out[:80].replace("\n", " "))
                _context._history.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
        on_token("\n(max tool steps reached)")
        outcome = "max_steps"
    finally:
        _busy[0] = False
        _observe._trace_end(outcome)
        _learn._maybe_learn(_context._history, user_message, outcome)


# Tool plugins (pi.dev extension idea, ported): one .py file per tool,
# placed on the config disk by the manager (/config/plugins). Convention:
#   DESC = "…"; PARAMS = {...}; REQUIRED = [...];  def run(**kwargs): ...
# The filename (without .py) becomes the tool name. The microVM is the sandbox.
PLUGIN_TOOLS = set()
PLUGIN_DIR = os.environ.get("PLUGIN_DIR", "/config/plugins")


def load_plugins():
    import importlib.util, sys
    if not os.path.isdir(PLUGIN_DIR):
        return
    for entry in sorted(os.listdir(PLUGIN_DIR)):
        path = os.path.join(PLUGIN_DIR, entry)
        syspath_add = None
        if os.path.isdir(path):
            # multi-file tool: folder <name>/ with entry file tool.py (or
            # __init__.py / <name>.py). The folder goes on sys.path so that
            # internal imports (import helper) work.
            name = os.path.basename(path)
            src = None
            for cand in ("tool.py", "__init__.py", name + ".py"):
                if os.path.isfile(os.path.join(path, cand)):
                    src = os.path.join(path, cand); break
            if not src:
                _config.log(f"plugin '{name}' ignored: no tool.py/__init__.py in the folder")
                continue
            syspath_add = path
        elif path.endswith(".py"):
            name, src = os.path.basename(path)[:-3], path
        else:
            continue
        if name in BUILTIN and name not in PLUGIN_TOOLS:
            _config.log(f"plugin '{name}' ignored: collides with a built-in tool")
            continue
        try:
            if syspath_add and syspath_add not in sys.path:
                sys.path.insert(0, syspath_add)
            spec = importlib.util.spec_from_file_location("plugin_" + name, src)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            BUILTIN[name] = (mod.run, str(getattr(mod, "DESC", name))[:300],
                             getattr(mod, "PARAMS", {}), getattr(mod, "REQUIRED", []))
            PLUGIN_TOOLS.add(name)
            _config.log(f"plugin loaded: {name}")
        except Exception as e:
            _config.log(f"plugin '{name}' ERROR: {e!r}")


def init():
    global TOOLS
    os.makedirs(_config.WORKDIR, exist_ok=True)
    load_plugins()
    TOOLS = builtin_schema() + _mcp.init_mcp()
    _config.log(f"agent ready: backend={_config.LLM_BACKEND} url={_mgrclient._llm_url()} model={_config.OR_MODEL} tools={len(TOOLS)} workdir={_config.WORKDIR}")


# ---- end of agent ----
