# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Tools that go through the manager: sub-agents and tasks, missions, the oracle, Home Assistant, notify and Signal, the inbox, the agent list, skills, memory, playbooks, secrets, and the katfs remote share.

Part of the openrouter agent package (runs inside the VM): no import from the package root. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import json
import os
import time
import urllib.error

from . import llm as _llm
from . import mgrclient as _mgrclient
from . import observe as _observe


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
