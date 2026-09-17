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
import datetime
import urllib.parse
import urllib.request
import urllib.error
import uuid

# ---- package modules ----
from . import learn as _learn
from . import llm as _llm
from . import mcp as _mcp
from . import tools_local as _tools_local
from . import observe as _observe
from . import mgrclient as _mgrclient
from . import config as _config

def t_spawn_subagent(task, model=None, tools=None, egress=None, skill=None, persona=None):
    """Delegate a self-contained subtask to a FRESH ephemeral VM and return its
    answer. Runs over the manager's task path (create_task target=ephemeral,
    wait=true) — the manager creates, drives and deletes the VM; the guest
    never touches the admin routes (which it may not call anyway). `model`
    picks the subagent's OpenRouter model, default: the template's.
    tools / egress / skill narrow the cage: a subset of this agent's tools,
    an egress allowlist (or "none"), one skill baked into the system prompt.
    With a skill and no tools, the sandbox gets the file/web tools only."""
    payload = {"message": str(task or "").strip(), "target": "ephemeral",
               "wait": True, "model": (model or "").strip()}
    sb = {}
    if tools:
        sb["tools"] = tools if isinstance(tools, list) else str(tools)
    if egress:
        sb["egress"] = egress if isinstance(egress, list) else str(egress)
    if skill:
        sb["skill"] = str(skill).strip()
    if persona:
        sb["persona"] = str(persona).strip()
    if sb:
        payload["sandbox"] = sb
    if not payload["message"]:
        return "⚠️ task missing"
    try:
        body = _mgrclient._mgr(_mgrclient._manager_base(), "/api/task", payload, timeout=630)
        d = json.loads(body)
    except Exception as e:
        return f"Subagent failed: {e!r}"
    if d.get("error"):
        return f"⚠️ {d['error']}"
    if "result" in d:
        return str(d["result"]) or "(subagent returned no result)"
    return "(subagent returned no result)"


def t_create_task(task, target="ephemeral", schedule="", wait=False, model=""):
    """Queue a task for execution — on a CAPABLE instance or
    isolated in an ephemeral VM. The manager runs it; the result
    appears in the shared chat history (app/web). `model` applies to
    ephemeral targets only (the VM is created with it)."""
    payload = {"message": task, "target": (target or "ephemeral").strip(),
               "schedule": (schedule or "").strip(), "wait": bool(wait),
               "model": (model or "").strip()}
    try:
        body = _mgrclient._mgr(_mgrclient._manager_base(), "/api/task", payload,
                    timeout=630 if wait else 30)
        d = json.loads(body)
        if d.get("error"):
            return f"⚠️ {d['error']}"
        if "result" in d:                      # wait=True -> result directly
            return str(d["result"])
        return (f"Task queued (id {d.get('id')}, target {d.get('target')}, "
                f"{d.get('status')}). The result will appear in the chat.")
    except Exception as e:
        return f"Error: {e!r}"


def t_mission_start(goal, steps):
    """Create a multi-stage assignment as a mission: goal + planned steps.
    The progress lives in the manager and survives restart/reset."""
    if isinstance(steps, str):
        steps = [x.strip() for x in steps.split("\n") if x.strip()]
    try:
        d = json.loads(_mgrclient._mgr(_mgrclient._manager_base(), "/api/mission-start",
                            {"goal": goal, "steps": steps}, timeout=10))
        return f"Mission {d['id']} created." if d.get("id") else f"Not created: {d.get('note','')}"
    except Exception as e:
        return f"Error: {e!r}"


def t_missions():
    """List active/paused missions with steps and status."""
    try:
        ms = json.loads(_mgrclient._mgr_get(_mgrclient._manager_base(), "/api/missions", timeout=8)).get("missions", [])
        if not ms:
            return "no missions"
        out = []
        for m in ms:
            if m.get("status") in ("done", "failed"):
                continue
            steps = " | ".join(f"{st['n']}[{st['status']}] {st['text'][:60]}"
                               + (f" (task {st['task_id']})" if st.get("task_id") else "")
                               for st in m.get("steps", []))
            out.append(f"{m['id']} [{m['status']}] {m['goal'][:80]} :: {steps}")
        return "\n".join(out) or "no open missions"
    except Exception as e:
        return f"Error: {e!r}"


def t_mission_update(id, step=None, status="", result="", task_id="", add_step="",
                     note="", target=""):
    """Advance a mission step: status open|doing|done|failed, result brief,
    record the task_id of the kicked-off task and the target instance it went
    to; add_step appends a new step; note only writes to the log."""
    try:
        body = {"id": id, "status": status, "result": result,
                "task_id": task_id, "add_step": add_step, "note": note,
                "target": target}
        if step is not None:
            body["step"] = int(step)
        d = json.loads(_mgrclient._mgr(_mgrclient._manager_base(), "/api/mission-update", body, timeout=10))
        return d.get("msg", "?")
    except Exception as e:
        return f"Error: {e!r}"


def t_mission_finish(id, summary, failed=False):
    """Finish a mission (or end it as failed with failed=true).
    The conclusion goes into long-term memory, the user gets a notification."""
    try:
        d = json.loads(_mgrclient._mgr(_mgrclient._manager_base(), "/api/mission-finish",
                            {"id": id, "summary": summary, "failed": bool(failed)}, timeout=10))
        return d.get("msg", "?")
    except Exception as e:
        return f"Error: {e!r}"


ORACLE_MODEL = os.environ.get("ORACLE_MODEL", "").strip()   # empty = current model
ORACLE_PROMPT = (
    "You are a skeptical advisor (Oracle): a second opinion BEFORE an action. "
    "You NEVER act yourself. Question the assumptions: Does the action fit the "
    "actual assignment? Is the target unambiguously identified (ID + content, "
    "not just time/name)? What would the damage be if the assumption is wrong? "
    "Answer concisely: first 'OBJECTION:' with the strongest counter-argument "
    "(or 'NO OBJECTION'), then at most 3 lines of reasoning/recommendation.")


def t_oracle(plan, kontext=""):
    """Second opinion before an action (pi.dev idea 'oracle'): challenge the
    assumptions, without acting yourself. An extra LLM call without tools; via
    ORACLE_MODEL optionally a stronger model."""
    msgs = [{"role": "system", "content": ORACLE_PROMPT},
            {"role": "user", "content": f"PLANNED ACTION:\n{plan}\n\nCONTEXT:\n{kontext or '(none)'}"}]
    r = _llm.or_chat(msgs, [], model=ORACLE_MODEL or None)
    return (r.get("content") or "").strip() or "(Oracle gave no answer — when in doubt do NOT act)"


def t_ha_control(spoken, action):
    """Turn a Home Assistant device or whole room on/off by the name you HEARD —
    the manager matches it against real entities and areas server-side (exact,
    then area, then closest-sounding) and auto-learns a spoken alias on a fuzzy
    hit, so the same wording is instant next time. PREFER this for voice light/
    device control over the raw homeassistant intents: pass the spoken target
    verbatim ('Gartenhaus denke rechts', 'Licht im Gartenhaus') and action
    'on'/'off'. It also handles rooms ('Licht im Gartenhaus' -> all lights of
    that area)."""
    try:
        return _mgrclient._mgr(_mgrclient._manager_base(), "/api/ha-control",
                    {"spoken": spoken, "action": action}, timeout=25)
    except urllib.error.HTTPError as e:
        return f"⚠️ HA control failed: HTTP {e.code}"
    except Exception as e:
        return f"⚠️ HA control failed: {e!r}"


def t_ha_learn_alias(spoken, entity):
    """Teach Home Assistant that a spoken/misheard name refers to an entity, so
    the SAME wording matches natively next time. Use this after you recovered
    from a failed HA intent: you heard e.g. 'Gartenhaus denke rechts', found the
    real entity 'light.gartenhaus_decke_rechts' via GetLiveContext, and switched
    it — then call ha_learn_alias('Gartenhaus denke rechts',
    'light.gartenhaus_decke_rechts'). The HA token stays on the host; you pass
    only the words and the entity id."""
    try:
        return _mgrclient._mgr(_mgrclient._manager_base(), "/api/ha-alias",
                    {"spoken": spoken, "entity": entity}, timeout=20)
    except urllib.error.HTTPError as e:
        return f"⚠️ alias not learned: HTTP {e.code}"
    except Exception as e:
        return f"⚠️ alias not learned: {e!r}"


def t_notify(title, message=""):
    """Send a push notification to the user's devices (app as an
    Android system notification, web manager as a bell). For important
    events/results when the user is not in the chat. Unlike
    send_signal (which rings in Signal), this is the app/web channel. Delivery
    goes through the manager."""
    try:
        body = _mgrclient._mgr(_mgrclient._manager_base(), "/api/notify",
                    {"title": title, "message": message}, timeout=15)
        d = json.loads(body)
        return "Notification sent." if d.get("id") else \
            "⚠️ not sent: " + str(d.get("note", ""))
    except urllib.error.HTTPError as e:
        try:
            return "⚠️ not sent: " + str(json.loads(e.read()).get("note", e.code))
        except Exception:
            return f"⚠️ not sent (HTTP {e.code})"
    except Exception as e:
        return f"⚠️ Error: {e!r}"


def t_send_signal(text, to=""):
    """Write to the user via Signal. Delivery runs in the manager: the
    bot number and the API access live there, and the recipient is checked
    against the list of allowed numbers. So from here you cannot
    write to arbitrary numbers — by design."""
    try:
        body = _mgrclient._mgr(_mgrclient._manager_base(), "/api/signal",
                    {"text": text, "to": (to or "").strip()}, timeout=45)
        d = json.loads(body)
        return ("Signal sent: " if d.get("ok") else "⚠️ not sent: ") + str(d.get("note", ""))
    except urllib.error.HTTPError as e:
        try:
            return "⚠️ not sent: " + str(json.loads(e.read()).get("note", e.code))
        except Exception:
            return f"⚠️ not sent: HTTP {e.code}"
    except Exception as e:
        return f"Error: {e!r}"


def t_read_inbox(peek=False):
    """Read new user messages (Signal/app/web) since the last run —
    the orchestrator's inbox. By default each message is delivered only
    ONCE (watermark). peek=True returns without 'consuming'."""
    try:
        body = _mgrclient._mgr_get(_mgrclient._manager_base(), "/api/inbox" + ("?peek=1" if peek else ""))
        msgs = json.loads(body).get("messages", [])
        if not msgs:
            return "Inbox empty (nothing new)"
        out = []
        for m in msgs:
            who = m.get("instance") or m.get("title") or "?"
            out.append(f"[{who}] {str(m.get('text',''))[:200]}")
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e!r}"


def t_list_agents():
    """List available agent instances + capabilities (model, MCP) —
    for routing: choose as the create_task target the agent that has the needed
    tools/MCP (e.g. the one with the homeassistant MCP for lights/heating)."""
    try:
        rows = json.loads(_mgrclient._mgr_get(_mgrclient._manager_base(), "/api/agents")).get("agents", [])
        if not rows:
            return "no agents"
        out = []
        for a in rows:
            mcp = (" mcp:" + ",".join(a["mcps"])) if a.get("mcps") else ""
            st = "running" if a.get("running") else "off"
            out.append(f"{a['name']} [{st}] {a.get('backend') or a.get('template','')} {a.get('model','')}{mcp}")
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e!r}"


def t_recall_tasks(query="", limit=10):
    """Query previously executed tasks (long-term memory / base knowledge).
    Without query the most recent; with query, search by text in task/result/goal.
    Use this BEFORE creating new tasks to avoid duplicates."""
    try:
        q = urllib.parse.quote(query or "")
        body = _mgrclient._mgr_get(_mgrclient._manager_base(), f"/api/history?q={q}&limit={int(limit)}")
        rows = json.loads(body).get("rows", [])
        if not rows:
            return "no matching earlier tasks"
        out = []
        for r in rows:
            ts = time.strftime("%m-%d %H:%M", time.localtime(r.get("ts", 0)))
            ok = "" if r.get("ok") else "⚠️ "
            out.append(f"[{ts}] {ok}{r.get('target')}: {str(r.get('task',''))[:80]}"
                       f" -> {str(r.get('result','') or '')[:140]}")
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e!r}"


def t_list_tasks():
    """List running/scheduled tasks with IDs — needed to remove a specific
    one with delete_task. (recall_tasks, by contrast, returns the history of
    completed runs, not the active ones with their IDs.)"""
    try:
        body = _mgrclient._mgr_get(_mgrclient._manager_base(), "/api/tasks-open")
        tasks = json.loads(body).get("tasks", [])
        if not tasks:
            return "no running tasks"
        out = []
        for t in tasks:
            sch = f" [{t['schedule']}]" if t.get("schedule") else ""
            out.append(f"{t.get('id')} @{t.get('instance')} ({t.get('status')}){sch}: "
                       f"{str(t.get('message',''))[:80]}")
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e!r}"


def t_delete_task(id):
    """Remove a running/scheduled task by ID. The ID comes from
    list_tasks. Final; it does not abort a task that is currently running,
    but prevents future runs."""
    try:
        body = _mgrclient._mgr(_mgrclient._manager_base(), "/api/task-delete", {"id": str(id)})
        d = json.loads(body)
        return (f"Task {id} deleted." if d.get("deleted")
                else f"No task with ID {id} found.")
    except Exception as e:
        return f"Error: {e!r}"


def t_edit_task(id, message="", schedule=""):
    """Change the message and/or schedule of a task (ID from list_tasks).
    schedule e.g. 'every 2h', 'daily 08:00', 'hourly'; an empty schedule turns
    a recurring task into a one-off. Empty fields stay
    unchanged. A task that is currently RUNNING cannot be changed."""
    try:
        payload = {"id": str(id)}
        if message:
            payload["message"] = message
        if schedule is not None:
            payload["schedule"] = schedule
        body = _mgrclient._mgr(_mgrclient._manager_base(), "/api/task-edit", payload)
        return str(json.loads(body).get("result", body))
    except Exception as e:
        return f"Error: {e!r}"


def t_list_skills(query=""):
    """List available expert skills. Without a query: names only (the catalog
    has ~70 entries; the full descriptions cost ~2.5k tokens per call). With a
    query: name + description of the matching ones."""
    try:
        arr = json.loads(_mgrclient._mgr_get(_mgrclient._manager_base(), "/api/skills?meta=1"))
    except Exception as e:
        return f"Error: {e!r}"
    if not arr:
        return "No skills available."
    q = (query or "").strip().lower()
    if q:
        hits = [s for s in arr
                if q in s.get("name", "").lower() or q in s.get("description", "").lower()]
        if not hits:
            return f"No skill matches '{query}'. list_skills() shows all names."
        return "\n".join(f"- {s.get('name')}: {s.get('description', '')}" for s in hits)
    names = sorted(s.get("name", "") for s in arr)
    return ("Skills (load with load_skill(name); descriptions via "
            "list_skills(query=…)):\n" + ", ".join(names))


def t_propose_skill(name, description, content):
    """Propose a skill for the catalog: a procedure that worked and will be
    needed again. It waits for the operator's approval in the Skills tab."""
    try:
        return _mgrclient._mgr(_mgrclient._manager_base(), "/api/skill-proposals",
                    {"name": name, "description": description, "content": content,
                     "turn": _observe._turn_id[0], "note": "proposed by the agent"}, timeout=10)
    except Exception as e:
        return f"Error: {e!r}"


def t_search_sessions(query, instance=""):
    """Exact (full-text) search over earlier chats and task results — the
    counterpart of memory_recall's semantic search."""
    try:
        raw = _mgrclient._mgr(_mgrclient._manager_base(), "/api/sessions-search",
                   {"q": query, "instance": instance or "", "limit": 10}, timeout=15)
        hits = json.loads(raw).get("hits", [])
    except Exception as e:
        return f"Error: {e!r}"
    if not hits:
        return f"No earlier session mentions '{query}'."
    out = []
    for h in hits:
        when = time.strftime("%Y-%m-%d", time.localtime(h.get("ts") or 0)) if h.get("ts") else "?"
        out.append(f"- {when} [{h.get('kind')}] {h.get('instance')} · {h.get('title', '')[:60]}: {h.get('snippet', '')}")
    return "\n".join(out)


def t_load_skill(name):
    """Load a skill into the context (returns the knowledge document)."""
    try:
        return _mgrclient._mgr_get(_mgrclient._manager_base(), f"/api/skills/{urllib.parse.quote(str(name), safe='')}")
    except Exception as e:
        return f"Error: {e!r}"


def t_memory_store(key, value):
    """Store a value permanently (centrally in the manager, survives instance deletion)."""
    inst = os.environ.get("FC_INSTANCE", "default")
    try:
        return _mgrclient._mgr(_mgrclient._manager_base(), f"/api/memory/{inst}", {"key": key, "value": value})
    except Exception as e:
        return f"Error: {e!r}"


def t_memory_reflect(question):
    """A reasoned answer from the second memory (Hindsight) over everything
    this instance has seen — chat turns and notes. Off unless the manager has
    HINDSIGHT_URL set; then the route says so."""
    try:
        d = json.loads(_mgrclient._mgr(_mgrclient._manager_base(), "/api/memory-reflect", {"query": question}, timeout=150))
        return d.get("text") or d.get("error") or "(no answer)"
    except Exception as e:
        return f"Error: {e!r}"


def t_memory_recall(key=None):
    """Retrieve a stored value (without key: all entries for this instance)."""
    inst = os.environ.get("FC_INSTANCE", "default")
    try:
        # Store takes the key via JSON body — ANY string works there. Recall
        # puts it into the URL path, so it must be quoted, or a key with a
        # space/umlaut can be stored but never retrieved (bit a live agent:
        # "jobsuche Firmen" saved fine, recall exploded).
        tail = f"/{urllib.parse.quote(str(key), safe='')}" if key else ""
        return _mgrclient._mgr_get(_mgrclient._manager_base(), f"/api/memory/{inst}" + tail)
    except Exception as e:
        return f"Error: {e!r}"


def t_playbook_add(rule):
    """Record a permanent rule/procedure (playbook). It will ALWAYS be
    surfaced and followed from now on."""
    try:
        d = json.loads(_mgrclient._mgr(_mgrclient._manager_base(), "/api/playbook-add", {"text": rule}))
        if d.get("added"):
            return "Rule saved."
        return "Rule already exists." if d.get("note") == "exists" else "Not saved."
    except Exception as e:
        return f"Error: {e!r}"


def t_playbooks():
    """Show all fixed rules (playbooks) with IDs."""
    try:
        pbs = json.loads(_mgrclient._mgr_get(_mgrclient._manager_base(), "/api/playbooks")).get("playbooks", [])
        if not pbs:
            return "no playbooks"
        return "\n".join(f"{p['id']}: {p['text']}" for p in pbs)
    except Exception as e:
        return f"Error: {e!r}"


def t_playbook_forget(id):
    """Remove a rule by ID (ID from playbooks)."""
    try:
        d = json.loads(_mgrclient._mgr(_mgrclient._manager_base(), "/api/playbook-remove", {"id": str(id)}))
        return f"Rule {id} removed." if d.get("removed") else f"No rule {id}."
    except Exception as e:
        return f"Error: {e!r}"


def t_list_secrets():
    """Show which secrets this agent may fetch according to the allowlist (names only)."""
    try:
        d = json.loads(_mgrclient._mgr_get(_mgrclient._manager_base(), "/api/secrets"))
        ks = d.get("allowed", [])
        return "Allowed secrets: " + (", ".join(ks) if ks else "(none)")
    except Exception as e:
        return f"Error: {e!r}"


def t_get_secret(name):
    """Fetch an allowed secret from the manager (only when needed; do not log/share)."""
    try:
        d = json.loads(_mgrclient._mgr_get(_mgrclient._manager_base(), f"/api/secret/{name}"))
        return d.get("value", "") if "value" in d else f"⚠️ {d.get('error', 'not allowed')}"
    except urllib.error.HTTPError as e:
        return "⚠️ not allowed" if e.code == 403 else f"Error: HTTP {e.code}"
    except Exception as e:
        return f"Error: {e!r}"


def t_remote_ls(path="."):
    """List the shared remote directory (P2P browser share)."""
    # No longer directly to the katfs node (which is loopback-only since the
    # isolation fix), but through the broker in the manager. It recognizes the
    # instance by its source IP and addresses ONLY its assigned share —
    # the agent can no longer reach someone else's.
    try:
        return _mgrclient._mgr_get(_mgrclient._manager_base(), f"/api/katfs/ls?path={urllib.parse.quote(path)}")
    except Exception as e:
        return f"Error (is the share active?): {e!r}"


def t_remote_read(path):
    """Read a file from the shared remote directory."""
    try:
        return _mgrclient._mgr_get(_mgrclient._manager_base(),
                        f"/api/katfs/read?path={urllib.parse.quote(path)}", timeout=60)
    except Exception as e:
        return f"Error: {e!r}"


def _katfs_post(url, data=b""):
    """POST to the katfs node. On HTTP errors take the body along — that is where
    the actual reason is ({"error": ...}); without it only a bare
    'Internal Server Error' remains, which is useless to both model and human."""
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        return urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        return f"Error HTTP {e.code}: {body or e.reason}"
    except Exception as e:
        return f"Error: {e!r}"


def t_remote_write(path, content):
    """Write a file to the shared remote directory."""
    return _katfs_post(
        _mgrclient._manager_base() + f"/api/katfs/write?path={urllib.parse.quote(path)}",
        (content or "").encode())


def t_remote_delete(path, recursive=False):
    """Delete a file/folder from the shared remote directory."""
    q = f"/api/katfs/delete?path={urllib.parse.quote(path)}"
    if recursive:
        q += "&recursive=1"
    return _katfs_post(_mgrclient._manager_base() + q)


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
    "spawn_subagent": (t_spawn_subagent,
                       "Delegate a self-contained subtask to a fresh ephemeral VM and wait for its answer "
                       "(the manager creates and deletes the VM). Optionally pick the subagent's model — "
                       "e.g. a cheap/fast one for grunt work or a strong one for hard reasoning.",
                       {"task": {"type": "string", "description": "task for the subagent (self-contained: it has no memory of this chat)"},
                        "model": {"type": "string", "description": "optional OpenRouter model id for the subagent, e.g. google/gemini-2.5-flash"},
                        "tools": {"type": "string", "description": "optional: comma-separated subset of your own tools the subagent may use (narrower cage), e.g. 'bash,read_file,write_file'"},
                        "egress": {"type": "string", "description": "optional: comma-separated hosts the subagent may reach, or 'none' for no network at all"},
                        "skill": {"type": "string", "description": "optional: a skill from list_skills baked into the subagent's system prompt; without `tools` it then gets only the file/web tools"},
                        "persona": {"type": "string", "description": "optional: a named agent persona (e.g. code-reviewer, security-reviewer) baked into the subagent's system prompt; its recommended tools/model apply unless you override them"}}, ["task"]),
    "create_task": (t_create_task,
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
    "mission_start": (t_mission_start,
                      "Create a multi-stage assignment as a mission (goal + steps). For anything "
                      "that needs several tasks/days — the progress survives restarts.",
                      {"goal": {"type": "string", "description": "goal of the mission"},
                       "steps": {"type": "array", "items": {"type": "string"},
                                 "description": "planned steps in order"}},
                      ["goal", "steps"]),
    "missions": (t_missions, "List open missions with steps/status.", {}, []),
    "mission_update": (t_mission_update,
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
    "mission_finish": (t_mission_finish,
                       "Finish a mission; failed=true on failure. Provide a short conclusion.",
                       {"id": {"type": "string"}, "summary": {"type": "string"},
                        "failed": {"type": "boolean"}}, ["id", "summary"]),
    "oracle": (t_oracle,
               "Second opinion BEFORE a risky/irreversible action: challenges your "
               "assumptions, never acts itself. plan = what you intend and why; kontext = "
               "relevant facts (IDs, wordings, user assignment). On 'OBJECTION' do not "
               "act, but resolve it or ask back.",
               {"plan": {"type": "string", "description": "planned action + reasoning"},
                "kontext": {"type": "string", "description": "facts: IDs, wordings, assignment"}},
               ["plan"]),
    "ha_control": (t_ha_control,
                   "Turn a Home Assistant device OR whole room on/off by the SPOKEN name "
                   "(manager matches real entities/areas server-side and auto-learns the "
                   "alias on a fuzzy hit). Prefer this over raw HA intents for voice control: "
                   "pass the heard target verbatim and action on/off. Handles rooms too "
                   "('Licht im Gartenhaus').",
                   {"spoken": {"type": "string", "description": "the spoken target, e.g. 'Gartenhaus denke rechts' or 'Licht im Gartenhaus'"},
                    "action": {"type": "string", "description": "'on' or 'off'"}},
                   ["spoken", "action"]),
    "ha_learn_alias": (t_ha_learn_alias,
                       "Teach Home Assistant a spoken-name alias for an entity so the same "
                       "misheard wording matches natively next time (STT hears 'Decke' as "
                       "'denke'). Call it after recovering from a failed HA intent, with the "
                       "words you originally heard and the real entity id.",
                       {"spoken": {"type": "string", "description": "the spoken/misheard name, e.g. 'Gartenhaus denke rechts'"},
                        "entity": {"type": "string", "description": "real entity id, e.g. 'light.gartenhaus_decke_rechts'"}},
                       ["spoken", "entity"]),
    "notify": (t_notify,
               "Push notification to the user's devices (app system notification + "
               "web-manager bell). For important events/results when they are not in the "
               "chat. Unlike send_signal this is the app/web channel, does not ring "
               "in Signal.",
               {"title": {"type": "string", "description": "short title"},
                "message": {"type": "string", "description": "text of the notification"}},
               ["title"]),
    "send_signal": (t_send_signal,
                    "Send the user a Signal message — for results, findings "
                    "or questions when they are not currently in the chat. Do NOT use for the "
                    "normal reply in an ongoing conversation (that arrives anyway) "
                    "and not repeatedly unprompted: a message rings on a "
                    "phone. Recipients only from the allowed list; leaving 'to' empty "
                    "means: to the default recipient.",
                    {"text": {"type": "string", "description": "message text"},
                     "to": {"type": "string", "description": "optional: number in the format +49…"}},
                    ["text"]),
    "read_inbox": (t_read_inbox,
                  "Read new user messages (Signal/app/web) since the last run — "
                  "the orchestrator's inbox. Each message comes only once (watermark); "
                  "peek=true to preview without consuming.",
                  {"peek": {"type": "boolean", "description": "only look, do not consume"}}, []),
    "list_agents": (t_list_agents,
                    "List available agent instances + capabilities (model/MCP). "
                    "For routing: choose the create_task target by capability.",
                    {}, []),
    "recall_tasks": (t_recall_tasks,
                     "Query previously executed tasks + results (long-term memory). "
                     "Without query the most recent, with query search specifically. Use BEFORE create_task "
                     "to check whether something is already done/scheduled (no duplicates).",
                     {"query": {"type": "string", "description": "search term (empty = most recent)"},
                      "limit": {"type": "integer", "description": "max hits (default 10)"}}, []),
    "list_tasks": (t_list_tasks,
                   "List RUNNING/scheduled tasks with IDs — for targeted deletion. "
                   "(recall_tasks, by contrast, is the history of completed runs.)", {}, []),
    "delete_task": (t_delete_task,
                    "Delete a running/scheduled task by ID. Get the ID first with "
                    "list_tasks. Final.",
                    {"id": {"type": "string", "description": "task ID from list_tasks"}}, ["id"]),
    "edit_task": (t_edit_task,
                  "Change the message and/or schedule of a task (ID from list_tasks). "
                  "schedule e.g. 'every 2h', 'daily 08:00', 'hourly'; empty = one-off.",
                  {"id": {"type": "string", "description": "task ID from list_tasks"},
                   "message": {"type": "string", "description": "new text (empty = unchanged)"},
                   "schedule": {"type": "string", "description": "new schedule (empty = one-off/unchanged)"}},
                  ["id"]),
    "search_sessions": (t_search_sessions,
                        "Full-text search over earlier chats and task results (exact words, "
                        "newest and best matches first). Use memory_recall for meaning, "
                        "this for names, numbers, URLs you remember seeing.",
                        {"query": {"type": "string", "description": "words to look for"},
                         "instance": {"type": "string", "description": "optional: another instance (orchestrator only)"}},
                        ["query"]),
    "propose_skill": (t_propose_skill,
                      "Propose a reusable procedure as a skill for the catalog (after a "
                      "non-trivial task that worked, or after the user corrected your approach). "
                      "The operator approves it in the Skills tab.",
                      {"name": {"type": "string", "description": "kebab-case name"},
                       "description": {"type": "string", "description": "one line: what it is for"},
                       "content": {"type": "string", "description": "Markdown: purpose, when to use, exact steps and tools, pitfalls; no secrets"}},
                      ["name", "description", "content"]),
    "list_skills": (t_list_skills,
                    "List available expert skills. Without arguments: names only. "
                    "query='…' searches names AND descriptions. Before specialized "
                    "tasks, check whether a matching skill exists.",
                    {"query": {"type": "string",
                               "description": "optional: filter, e.g. 'docker' or 'security'"}},
                    []),

    "load_skill": (t_load_skill, "Load an expert skill (knowledge document) into the context and follow it.",
                   {"name": {"type": "string", "description": "skill name from list_skills"}}, ["name"]),
    "memory_store": (t_memory_store, "Store a value permanently (survives restart/instance deletion).",
                     {"key": {"type": "string"}, "value": {"type": "string"}}, ["key", "value"]),
    "memory_recall": (t_memory_recall, "Retrieve a stored value; without key all entries.",
                      {"key": {"type": "string"}}, []),
    "memory_reflect": (t_memory_reflect,
                       "Ask the long-term memory a question and get a reasoned answer over everything "
                       "remembered (past conversations, notes). Use for 'what do we know about…', "
                       "'what did the user say about…', preferences and history.",
                       {"question": {"type": "string"}}, ["question"]),
    "playbook_add": (t_playbook_add,
                     "Record a permanent rule/procedure — applies ALWAYS from now on. "
                     "Use this when the user tells you HOW something is to be done, states a "
                     "lasting preference or corrects you.",
                     {"rule": {"type": "string", "description": "the rule as a short, concrete sentence"}}, ["rule"]),
    "playbooks": (t_playbooks, "Show all fixed rules (playbooks) with IDs.", {}, []),
    "playbook_forget": (t_playbook_forget, "Remove a rule by ID (ID from playbooks).",
                        {"id": {"type": "string", "description": "playbook ID"}}, ["id"]),
    "remote_ls": (t_remote_ls,
                  "List the folder the user has shared (lives on THEIR machine, "
                  "connected via P2P). Paths are relative to the root of the share.",
                  {"path": {"type": "string", "description": "relative, default '.'"}}, []),
    "remote_read": (t_remote_read,
                    "Read a file from the user's shared folder (path relative to the share).",
                    {"path": {"type": "string"}}, ["path"]),
    "remote_write": (t_remote_write,
                     "Write a file to the user's shared folder — CREATES and "
                     "OVERWRITES, missing subfolders are created automatically. Write access "
                     "is explicitly allowed: when the user wants to put, save or "
                     "change something there, CALL THIS TOOL instead of claiming you cannot "
                     "write. Only if it returns an error is it not possible.",
                     {"path": {"type": "string", "description": "relative to the share, e.g. 'note.txt'"},
                      "content": {"type": "string", "description": "complete new file content"}},
                     ["path", "content"]),
    "remote_delete": (t_remote_delete,
                      "Delete a file or folder in the user's shared folder. "
                      "Irreversible — there is no trash. Only delete when the user "
                      "requests it, and ask first when in doubt. A non-empty folder "
                      "fails on purpose; set recursive=true for that.",
                      {"path": {"type": "string", "description": "relative to the share"},
                       "recursive": {"type": "boolean",
                                     "description": "delete the folder including its contents (default false)"}},
                      ["path"]),
    "list_secrets": (t_list_secrets, "Show the secret names released for this agent (no values).",
                     {}, []),
    "get_secret": (t_get_secret, "Fetch a released secret (e.g. API key/token) only when needed. Never output values in replies/logs.",
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
    return _finalize_output(name, out)


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


# --- 3) Context-Offloader ---------------------------------------------------
OFFLOAD_DIR = os.path.join(_config.WORKDIR, ".offload")
OFFLOAD_MIN = int(os.environ.get("OFFLOAD_MIN", str(_config.MAX_TOOL_OUT)))
OFFLOAD_PREVIEW = int(os.environ.get("OFFLOAD_PREVIEW", "2000"))
_offload_seq = 0


# Type-aware previews (idea from Caveman's per-type compressors, done in ~60
# lines of stdlib instead of adopting the BSL-licensed engine): the preview an
# agent sees for an offloaded output should carry STRUCTURE, not just the first
# N characters. A head-slice of a 40k JSON is usually an unclosed brace of the
# first record; an outline of keys, types and counts tells the model what it is
# holding and where to read on. Nothing is lost either way — the full text
# stays in the offload file.

def _preview_json(out, budget):
    """Outline of a JSON payload: shape, keys, counts, first items."""
    data = json.loads(out)      # caller catches
    lines = []

    def walk(node, path, depth):
        if len(lines) > 60 or depth > 3:
            return
        if isinstance(node, dict):
            lines.append(f"{path or '$'}: object, {len(node)} keys: "
                         + ", ".join(list(node.keys())[:12])
                         + (" …" if len(node) > 12 else ""))
            for k in list(node.keys())[:6]:
                v = node[k]
                if isinstance(v, (dict, list)):
                    walk(v, f"{path}.{k}" if path else k, depth + 1)
        elif isinstance(node, list):
            lines.append(f"{path or '$'}: array, {len(node)} items")
            if node and isinstance(node[0], (dict, list)):
                walk(node[0], (path or "$") + "[0]", depth + 1)
            elif node:
                sample = json.dumps(node[:3], ensure_ascii=False)
                lines.append(f"{path or '$'}[0..2]: {sample[:200]}")
        else:
            lines.append(f"{path or '$'}: {json.dumps(node, ensure_ascii=False)[:120]}")

    walk(data, "", 0)
    head = json.dumps(data, ensure_ascii=False)[:budget // 3]
    return ("[JSON structure]\n" + "\n".join(lines))[:budget - len(head) - 20] \
        + "\n\n[begins] " + head


def _preview_log(out, budget):
    """Head + tail + everything that smells like a problem, duplicates folded."""
    lines = out.splitlines()
    folded, last, count = [], None, 0
    for ln in lines:
        if ln == last:
            count += 1
            continue
        if count > 1:
            folded.append(f"  [previous line repeats ×{count}]")
        folded.append(ln)
        last, count = ln, 1
    if count > 1:
        folded.append(f"  [previous line repeats ×{count}]")
    interesting = [ln for ln in folded
                   if re.search(r"error|warn|fail|exception|traceback|fatal|denied",
                                ln, re.I)]
    head = folded[:15]
    tail = folded[-10:] if len(folded) > 25 else []
    mid = [ln for ln in interesting if ln not in head and ln not in tail][:20]
    parts = head + (["  […]"] if mid or tail else []) + mid \
        + (["  […]"] if tail and mid else []) + tail
    return (f"[log, {len(lines)} lines, duplicates folded]\n"
            + "\n".join(parts))[:budget]


def _smart_preview(out, budget):
    """Pick a preview by payload type; plain head-slice as the fallback."""
    stripped = out.lstrip()
    if stripped[:1] in "[{":
        try:
            return _preview_json(out, budget)
        except Exception:
            pass
    lines = out.count("\n")
    if lines >= 30 and len(out) / max(lines, 1) < 400:
        try:
            return _preview_log(out, budget)
        except Exception:
            pass
    return out[:budget]


def _finalize_output(name, out):
    """If a tool output is larger than OFFLOAD_MIN, it is offloaded to a file IN
    FULL and only a preview + reference is kept in the context (offload_read
    fetches the rest). This way nothing is lost without flooding the context.
    Smaller -> unchanged."""
    out = out if isinstance(out, str) else str(out)
    if len(out) <= OFFLOAD_MIN:
        return out
    global _offload_seq
    _offload_seq += 1
    oid = f"{name}-{_offload_seq}-{uuid.uuid4().hex[:6]}"
    try:
        os.makedirs(OFFLOAD_DIR, exist_ok=True)
        with open(os.path.join(OFFLOAD_DIR, oid + ".txt"), "w") as fh:
            fh.write(out)
    except Exception:
        return out[:_config.MAX_TOOL_OUT]   # offloading failed -> fall back: hard-truncate
    preview = _smart_preview(out, OFFLOAD_PREVIEW)
    return (preview + f"\n\n[… full output offloaded ({len(out)} characters). "
            f"Read verbatim with offload_read(id=\"{oid}\", offset=0).]")


def t_offload_read(id="", offset=0, length=None):
    """Read an offloaded tool output (see the offload reference) in chunks."""
    length = int(length) if length else _config.MAX_TOOL_OUT
    offset = max(0, int(offset or 0))
    safe = os.path.basename(str(id))              # no path traversal
    fp = os.path.join(OFFLOAD_DIR, safe + ".txt")
    try:
        with open(fp) as fh:
            fh.seek(offset)
            data = fh.read(length)
    except FileNotFoundError:
        return f"offload '{id}' not found."
    except Exception as e:
        return f"offload error: {e!r}"
    more = f"\n\n[… continue with offset={offset + len(data)} …]" if len(data) >= length else ""
    return data + more


# Attach offload_read to the tool catalog (only here, because t_offload_read
# is defined after the BUILTIN literal).
BUILTIN["offload_read"] = (
    t_offload_read,
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
    if AUTO_RESET_MIN <= 0 or not last or now - last < AUTO_RESET_MIN * 60 or len(_history) <= 1:
        return False
    del _history[1:]
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
DEADLINE_MARGIN = 45           # seconds before the deadline reserved for the final answer
_DEADLINE_NOTE = ("[TimeBudget] The time budget for this turn is exhausted. Answer NOW with "
                  "what you have: a partial result is fine, say what is still missing. "
                  "No more tools.")


def _out_of_time():
    return bool(_deadline[0]) and time.time() > _deadline[0] - DEADLINE_MARGIN


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
    _deadline[0] = float(deadline or 0)
    ts = _turn_steps(user_message)
    if ts:
        saved, _config.MAX_STEPS = _config.MAX_STEPS, ts[0]      # config's module global, per turn
        try:
            return run(ts[1], kind=kind, turn=turn)
        finally:
            _config.MAX_STEPS = saved
    _observe._turn_id[0] = turn or uuid.uuid4().hex[:8]
    user_message = _expand_prompt(user_message)
    if user_message.strip() == "/reset":
        del _history[1:]
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
        return _branch_open(user_message)
    if user_message.startswith("/back"):
        return _branch_close(user_message)
    # /fresh: run statelessly in a throwaway context — the conversation
    # _history stays untouched (otherwise a heartbeat would wipe out a running
    # app chat, because both share the same _history). For the
    # orchestrator heartbeat: look, delegate, discard.
    if user_message.startswith("/fresh"):
        m = user_message[len("/fresh"):].strip()
        hist = [{"role": "system", "content": _config.SYSTEM},
                {"role": "system", "content": _now_line()},
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
    _trim_history()
    _inject_playbooks()
    _inject_missions()
    _inject_memory_index()
    _inject_now()
    _recall(user_message)
    _history.append({"role": "user", "content": user_message})
    _busy[0] = True
    _observe._trace_begin(kind)
    out = "⚠️ (no answer)"
    try:
        out = _run_goal(_history, user_message) if _goal else _tool_loop(_history)
        return out
    finally:
        _busy[0] = False
        _observe._trace_end(_learn._outcome_of(out))
        _learn._maybe_learn(_history, user_message, _learn._outcome_of(out))


def run_stream(user_message, on_token, image=None, deadline=0.0, kind="stream", turn=None):
    _deadline[0] = float(deadline or 0)
    _observe._turn_id[0] = turn or uuid.uuid4().hex[:8]
    """Like run(), but streams the answer tokens via on_token. Tool rounds
    produce no text; the final answer is streamed.
    image: optional base64 JPEG -> sent as vision content to OpenRouter."""
    user_message = _expand_prompt(user_message)
    if user_message.strip() == "/reset":
        del _history[1:]
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
        on_token(_branch_open(user_message))
        return
    if user_message.startswith("/back"):
        on_token(_branch_close(user_message))
        return
    # /fresh: stateless as in run(), conversation untouched. Heartbeats
    # need no streaming — emit the answer once.
    if user_message.startswith("/fresh"):
        m = user_message[len("/fresh"):].strip()
        on_token(_tool_loop([{"role": "system", "content": _config.SYSTEM},
                             {"role": "user", "content": m}]))
        return
    _auto_reset()
    _trim_history()
    _inject_playbooks()
    _inject_missions()
    _inject_memory_index()
    _inject_now()
    _recall(user_message)
    if image:
        content = [
            {"type": "text", "text": user_message or "What is in the image?"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
        ]
    else:
        content = user_message
    _history.append({"role": "user", "content": content})
    _busy[0] = True
    _observe._trace_begin(kind)
    outcome = "error"
    try:
        if _goal:
            # With an active goal the answer is refined against the judge (not
            # streamed) and then emitted as a whole.
            ans = _run_goal(_history, user_message)
            on_token(ans)
            outcome = _learn._outcome_of(ans)
            return
        for _ in _config._step_iter():
            _drain_steer(_history, on_token)
            if _out_of_time():
                _history.append({"role": "system", "content": _DEADLINE_NOTE})
                _history.append(_llm.or_chat_stream(_history, [], on_token))
                on_token("\n\n⏱️ (time budget exhausted — partial result)")
                outcome = "deadline"
                return
            msg = _llm.or_chat_stream(_history, TOOLS, on_token)
            _history.append(msg)
            tcs = msg.get("tool_calls")
            if not tcs:
                if _drain_steer(_history, on_token):
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
                _history.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
        on_token("\n(max tool steps reached)")
        outcome = "max_steps"
    finally:
        _busy[0] = False
        _observe._trace_end(outcome)
        _learn._maybe_learn(_history, user_message, outcome)


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
