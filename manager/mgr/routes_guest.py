# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""HTTP routes a guest VM may call (security boundary): everything registered without admin=True. A guest is identified by its source IP (mgr/guests); every route here is scoped to that instance. Admin-only routes live in mgr/routes_admin.py.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import json
import re
import urllib.request

from mgr import audit as _audit
from mgr import chats as _chats
from mgr import gateway as _gateway
from mgr import guests as _guests
from mgr import haalias as _haalias
from mgr import hindsight as _hindsight
from mgr import instances as _instances
from mgr import irohgw as _irohgw
from mgr import katfs as _katfs
from mgr import mcp as _mcp
from mgr import memfs as _memfs
from mgr import missions as _missions
from mgr import models as _models
from mgr import mounts as _mounts
from mgr import notify as _notify
from mgr import personas as _personas
from mgr import plugins as _plugins
from mgr import policy as _policy
from mgr import routes as _routes
from mgr import rules as _rules
from mgr import saddler as _saddler_mod
from mgr import secrets as _secrets
from mgr import settings as _settings
from mgr import signal as _signal_mod
from mgr import skills as _skills
from mgr import store as _store
from mgr import tasks as _tasks
from mgr import ui as _ui
from mgr import util as _util
from mgr import voice as _voice


@_routes.ROUTER.get("/api/personas")
def _rt_personas(h):
    return json.dumps(_personas.load_personas(), ensure_ascii=False).encode(), "application/json"


@_routes.ROUTER.get("/api/skills")
def _rt_skills(h):
    # ?meta=1: name + description only. The full catalog is ~870 KB with the
    # bodies — the agents call this on every list_skills and never need them.
    q = urllib.parse.parse_qs(h.path.partition("?")[2])
    items = _skills.load_skills()
    if q.get("meta", ["0"])[0] == "1":
        items = [{"name": x.get("name", ""), "description": x.get("description", "")}
                 for x in items]
    return json.dumps(items, ensure_ascii=False).encode(), "application/json"


@_routes.ROUTER.get("/api/skills/", prefix=True)
def _rt_skill(h):
    nm = re.sub(r"[^a-z0-9_-]", "", h.path.split("/api/skills/", 1)[1].lower())
    sk = next((x for x in _skills.load_skills() if x.get("name") == nm), None)
    return ((sk.get("content", "") if sk else f"Skill '{nm}' not found").encode(),
            "text/plain; charset=utf-8")


@_routes.ROUTER.get("/api/saddler")
def _rt_saddler(h):
    # Weekly failure digest over ALL instances' audits. That is cross-instance
    # information, so guests may not read it — except the orchestrator, whose
    # scheduled saddler task is the intended consumer.
    g = _guests.instance_by_ip(h.client_address[0])
    if g is not None and g.get("name") != _guests.ORCH_INSTANCE:
        return json.dumps({"error": "orchestrator only"}).encode(), "application/json"
    q = urllib.parse.parse_qs(h.path.partition("?")[2])
    try:
        days = max(1, min(int(q.get("days", ["7"])[0]), 60))
    except ValueError:
        days = 7
    d = _saddler_mod.digest(days)
    d["text"] = _saddler_mod.render(d)
    return json.dumps(d, ensure_ascii=False).encode(), "application/json"


@_routes.ROUTER.get("/api/websearch")
def _rt_websearch(h):
    # Web search for the agents: the Brave key stays on the host, the VM only
    # ever sees results. Same principle as the LLM key proxy. Metered per
    # guest: the key's quota is shared by every instance.
    from mgr import websearch
    g = h._guest()
    if g is not None and not _policy.tool_allowed(g, "web_search"):
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


@_routes.ROUTER.get("/api/prompts")
def _rt_prompts(h):
    return (json.dumps({"prompts": _rules.load_prompts()}, ensure_ascii=False).encode(),
            "application/json")


@_routes.ROUTER.get("/api/iroh")
def _rt_iroh_status(h):
    return json.dumps(_irohgw.status()).encode(), "application/json"


@_routes.ROUTER.get("/api/voice-health")
def _rt_voice_health(h):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{_settings.VOICE_PORT}/health", timeout=5) as r:
            out = r.read()
    except Exception as e:
        out = json.dumps({"ready": False, "error": str(e)}).encode()
    return out, "application/json"


@_routes.ROUTER.get("/logo.svg")
@_routes.ROUTER.get("/favicon.ico")
def _rt_logo(h):
    return _ui.LOGO_SVG.encode(), "image/svg+xml"



@_routes.ROUTER.post("/api/llm/", prefix=True)
def _rt_llm_proxy(h):
    # LLM key injection: streamed, so it answers itself.
    return h._llm_proxy(h.path.split("?", 1)[0])


# ---- secrets, credentials, MCP config (guests, by source IP) ----------------
@_routes.ROUTER.get("/api/secrets")
def _rt_secrets(h):
    # What get_secret may fetch: released AND guest-readable.
    inst = h._guest()
    keys = sorted(_secrets.guest_readable_keys(inst)) if inst else []
    return h._json({"allowed": keys, "instance": inst.get("name") if inst else None})


@_routes.ROUTER.get("/api/claude-credentials")
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


@_routes.ROUTER.get("/api/secret/", prefix=True)
def _rt_secret(h):
    name = h.path.split("/api/secret/", 1)[1]
    inst = h._guest()
    if inst is None or name not in _secrets.guest_readable_keys(inst):
        return h._json({"error": "not allowed (released for the hub only, or not released)"}, 403)
    return h._json({"value": _secrets.secret_store().get(name, "")})


@_routes.ROUTER.get("/api/mcp-config")
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


@_routes.ROUTER.post("/api/mcp")
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
@_routes.ROUTER.get("/api/agent-tools")
def _rt_agent_tools(h):
    return h._json({"tools": _policy.AGENT_TOOLS_CATALOG})


@_routes.ROUTER.get("/api/agents")
def _rt_agents(h):
    # Roster for routing (list_agents). Capabilities only — no secrets. A
    # guest lists only what it may delegate to; ephemeral children hidden.
    guest = h._guest()
    roster = []
    for i in _instances.load_instances():
        if i["name"].startswith(("task-", "sub-")):
            continue
        if guest is not None and not _guests.guest_may_target(guest, i["name"]):
            continue
        cfg = i.get("config") or {}
        mkey = next((k for k in _instances.MODEL_KEYS if cfg.get(k)), "")
        # Backend from the set model key, not the template (which stays
        # "openrouter" after a switch to orcarouter/llama via set_model).
        backend = {v: k for k, v in _instances.PROVIDER_MODEL_KEY.items()}.get(mkey, i.get("template", ""))
        if cfg.get("LLAMA_ENDPOINT"):
            backend = "llama"
        roster.append({"name": i["name"], "template": i.get("template", ""),
                       "backend": backend, "running": _instances.is_running(i),
                       "model": cfg.get(mkey, "") if mkey else "",
                       "mcps": [n for n in (cfg.get("MCP_SERVERS", "") or "").split(",") if n]})
    return h._json({"agents": roster})


@_routes.ROUTER.get("/api/inbox")
def _rt_inbox(h):
    # EVERY user message of every chat — among guests only the orchestrator.
    guest = h._guest()
    if guest is not None and guest["name"] != _guests.ORCH_INSTANCE:
        return h._forbid()
    peek = _routes._qs(h).get("peek", ["0"])[0] == "1"
    return h._json({"messages": _chats.inbox_since(peek=peek)})


@_routes.ROUTER.get("/api/missions")
def _rt_missions(h):
    # Guest: only its OWN missions. Admin: ?instance= or all.
    g = h._guest()
    if g is not None:
        return h._json({"missions": _missions.mission_list(g["name"])})
    inst = _routes._qs(h).get("instance", [""])[0]
    return h._json({"missions": _missions.mission_list(inst)} if inst else {"by_instance": _missions.load_missions()})


@_routes.ROUTER.get("/api/playbooks")
def _rt_playbooks(h):
    g = h._guest()
    inst = g["name"] if g else _routes._qs(h).get("instance", [""])[0]
    return h._json({"playbooks": _rules.pb_list(inst)})


@_routes.ROUTER.get("/api/tasks-open")
def _rt_tasks_open(h):
    g = h._guest()
    if g is not None and g.get("name") != _guests.ORCH_INSTANCE:
        return h._json({"error": "orchestrator only"}, 403)
    rows = [{"id": t.get("id"), "instance": t.get("instance"),
             "schedule": t.get("schedule", ""), "status": t.get("status", ""),
             "next_run": t.get("next_run", 0), "message": str(t.get("message", ""))[:200]}
            for t in _store.load_tasks()]
    return h._json({"tasks": rows})


@_routes.ROUTER.get("/api/history")
def _rt_history(h):
    # Guest: only runs it created or executed; orchestrator and admin: all.
    q = _routes._qs(h)
    guest = h._guest()
    scope = guest["name"] if guest is not None and guest["name"] != _guests.ORCH_INSTANCE else None
    return h._json({"rows": _store.history_search(q.get("q", [""])[0], q.get("limit", ["20"])[0],
                                           instance=scope)})


@_routes.ROUTER.get("/api/hitl/", prefix=True)
def _rt_hitl_status(h):
    hid = _routes._tail(h, "/api/hitl/")[0].strip()
    guest = h._guest()
    return h._json({"status": _signal_mod.hitl_status(hid, guest["name"] if guest else None)})


@_routes.ROUTER.get("/api/memory/", prefix=True)
def _rt_memory_get(h):
    # Keys may carry spaces/umlauts; a slash inside a key stays one key. A
    # guest reads only its OWN memory — the name comes from the source IP.
    seg = _routes._tail(h, "/api/memory/")
    if len(seg) > 2:
        seg = [seg[0], "/".join(seg[1:])]
    guest = h._guest()
    inst = guest["name"] if guest else seg[0]
    if len(seg) >= 2 and seg[1]:
        return h._json({"value": _store.mem_recall(inst, seg[1])})
    return h._json(_store.mem_recall(inst))


@_routes.ROUTER.post("/api/memory/", prefix=True)
def _rt_memory_post(h):
    b = h._body()
    guest = h._guest()
    target = guest["name"] if guest else _routes._tail(h, "/api/memory/")[0]
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


@_routes.ROUTER.post("/api/memory-search")
def _rt_memory_search(h):
    b = h._body()
    guest = h._guest()
    target = guest["name"] if guest else (b.get("instance") or "")
    hits = _store.sem_search(target, b.get("query", ""), b.get("k", 5)) if target else []
    if target and _hindsight.enabled():
        seen = {x["text"] for x in hits}
        hits += [x for x in _hindsight.recall(target, b.get("query", ""), b.get("k", 5)) if x["text"] not in seen]
    return h._json({"hits": hits})


@_routes.ROUTER.post("/api/memory-reflect")
def _rt_memory_reflect(h):
    # memory_reflect tool: a reasoned answer from the instance's Hindsight
    # bank. Guests get their own bank only; the admin may name an instance.
    b = h._body()
    guest = h._guest()
    target = guest["name"] if guest else (b.get("instance") or "")
    if not target:
        return h._json({"error": "instance missing"}, 400)
    return h._json({"text": _hindsight.reflect(target, b.get("query", ""))})


@_routes.ROUTER.post("/api/task")
def _rt_task_create_guest(h):
    # create_task tool: the caller is identified by source IP and chooses the
    # TARGET, not its identity. Ephemeral children may not create tasks.
    inst = h._guest()
    body = h._body()
    if inst is None:
        return h._json({"error": "guests only"}, 403)
    if inst["name"].startswith(("task-", "sub-")):
        return h._json({"error": "ephemeral VMs may not create tasks"})
    target, terr = _tasks.resolve_task_target(body.get("target"))
    message = str(body.get("message", "")).strip()
    schedule = str(body.get("schedule", "")).strip()
    model = str(body.get("model") or "").strip()[:120]   # ephemeral only
    if not message:
        return h._json({"error": "message missing"})
    if terr:
        return h._json({"error": terr})
    if not _guests.guest_may_target(inst, target):
        return h._json({"error": f"target '{target}' not allowed for this instance "
                                 "(own name, 'ephemeral', or a DELEGATE_TARGETS entry in its config)"})
    sandbox = None
    if body.get("sandbox"):
        if target != "ephemeral":
            return h._json({"error": "sandbox applies to ephemeral targets only"})
        scfg, sinternet, serr = _policy.sandbox_config(inst, body.get("sandbox"))
        if serr:
            return h._json({"error": f"sandbox: {serr}"})
        sandbox = {"cfg": scfg, "internet": sinternet}
    if body.get("wait") and not schedule:
        ok, res = _tasks._run_task_now(target, message, model, sandbox=sandbox)
        _store.history_add(target, message, res, ok, origin=inst["name"])
        return h._json({"ok": ok, "result": res})
    t = _store.add_task(target, message, schedule, model=model, sandbox=sandbox)
    return h._json({"id": t["id"], "status": t["status"], "target": target})


def _orchestrator_or_admin(h):
    g = h._guest()
    return g is None or g.get("name") == _guests.ORCH_INSTANCE


@_routes.ROUTER.post("/api/task-edit")
def _rt_task_edit(h):
    if not _orchestrator_or_admin(h):
        return h._json({"error": "orchestrator only"}, 403)
    b = h._body()
    return h._json({"result": _store.update_task(str(b.get("id") or ""), b.get("message"), b.get("schedule"))})


@_routes.ROUTER.post("/api/task-delete")
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


@_routes.ROUTER.post("/api/playbook-add")
@_routes.ROUTER.post("/api/playbook-remove")
def _rt_playbook_edit(h):
    b = h._body()
    g = h._guest()
    inst = g["name"] if g else (b.get("instance") or "")
    if h.path.split("?", 1)[0].endswith("add"):
        r = _rules.pb_add(inst, b.get("text") or b.get("rule") or "")
        return h._json({"id": r, "added": bool(r and r != "exists"), "note": r})
    return h._json({"removed": _rules.pb_remove(inst, b.get("id") or "")})


@_routes.ROUTER.post("/api/mission-start")
@_routes.ROUTER.post("/api/mission-update")
@_routes.ROUTER.post("/api/mission-finish")
def _rt_mission_write(h):
    # Every persistent agent (its own missions) or admin. Ephemeral VMs are
    # excluded — deleted after the task, their mission would dangle.
    g = h._guest()
    if g is not None and g["name"].startswith(("task-", "sub-")):
        return h._json({"error": "ephemeral VMs may not own missions"}, 403)
    inst = g["name"] if g else _guests.ORCH_INSTANCE
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


@_routes.ROUTER.post("/api/usage")
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


@_routes.ROUTER.post("/api/trace")
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


@_routes.ROUTER.post("/api/audit")
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


@_routes.ROUTER.post("/api/notify")
def _rt_notify(h):
    body = h._body()
    inst = h._guest()
    if inst is not None and not _policy.tool_allowed(inst, "notify"):
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
            _chats.chat_log_append(nm, "", "", f"🔔 {rtitle}\n\n{rtext}", kind="task")
        except Exception as e:
            print(f"[quiet] notify -> task chat failed: {e!r}", flush=True)
    try:
        # The WHY travels along ("empty" / "rate limit: …").
        _audit.audit_append(nm, "notify", (body.get("title") or "")[:60], bool(nid), err="" if nid else str(note))
    except Exception:
        pass
    return h._json({"id": nid, "note": note}, 200 if nid else 429)


@_routes.ROUTER.post("/api/hitl")
def _rt_hitl_create(h):
    body = h._body()
    inst = h._guest()
    hid = _signal_mod.hitl_create(inst["name"] if inst else "admin", str(body.get("tool", ""))[:40],
                      str(body.get("target", ""))[:200])
    return h._json({"id": hid})


@_routes.ROUTER.post("/api/signal")
def _rt_signal_send(h):
    # Recipient checked against ALLOWED_SENDERS, bot number from settings —
    # the VM knows neither.
    body = h._body()
    inst = h._guest()
    if inst is not None and not _policy.tool_allowed(inst, "send_signal"):
        return h._json({"ok": False, "note": "send_signal not allowed for this instance"}, 403)
    ok, note = _signal_mod.signal_send(body.get("text") or body.get("message"), body.get("to"))
    try:
        _audit.audit_append(inst["name"] if inst else "admin", "send_signal", (body.get("to") or "default"), ok)
    except Exception:
        pass
    return h._json({"ok": ok, "note": note}, 200 if ok else 400)


@_routes.ROUTER.post("/api/chat-log")
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
            _chats.chat_log_append(inst["name"], body.get("sender", ""), body.get("user", ""), body.get("reply", ""))
        except Exception as e:
            print(f"[quiet] chat_log_append failed: {e!r}", flush=True)
        try:
            _tasks.orchestrator_ping()   # Signal message -> orchestrator immediately
        except Exception:
            pass
    h.send_response(204); h.end_headers()


# ---- voice ----------------------------------------------------------------
@_routes.ROUTER.post("/api/stt")
@_routes.ROUTER.post("/api/tts")
def _rt_voice(h):
    # The voice service listens on loopback; the manager is the only door and
    # passes raw audio / WAV through unchanged.
    p = h.path.split("?", 1)[0]
    payload = h._raw(_routes.BODY_MAX_AUDIO)
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
@_routes.ROUTER.get("/api/katfs/ls")
@_routes.ROUTER.get("/api/katfs/read")
def _rt_katfs_guest_fs(h):
    inst = h._guest()
    if inst is None:
        return h._json({"error": "guests only"}, 403)
    op = "ls" if h.path.split("?", 1)[0].endswith("/ls") else "read"
    st, ct, data = _katfs._katfs_answer(h, op, _katfs.katfs_share_for(inst), _routes._qs(h).get("path", ["."])[0])
    h._send(data, ct, st)


@_routes.ROUTER.post("/api/katfs/write")
@_routes.ROUTER.post("/api/katfs/delete")
def _rt_katfs_guest_write(h):
    inst = h._guest()
    if inst is None:
        return h._json({"error": "guests only"}, 403)
    q = _routes._qs(h)
    path, share = q.get("path", [""])[0], _katfs.katfs_share_for(inst)
    ln = int(h.headers.get("Content-Length", 0) or 0)
    if h.path.split("?", 1)[0].endswith("/write"):
        if ln > _katfs.KATFS_MAX_WRITE:
            return h._json({"error": "too large"}, 413)
        st, ct, data = _katfs._katfs_answer(h, "write", share, path, False, h.rfile.read(ln) if ln else b"")
    else:
        if ln:
            h.rfile.read(ln)
        st, ct, data = _katfs._katfs_answer(h, "delete", share, path, q.get("recursive", ["0"])[0] == "1", None)
    h._send(data, ct, st)


@_routes.ROUTER.get("/api/models")
def _rt_models(h):
    return h._json({"curated": sorted(_models.load_curated())})


@_routes.ROUTER.get("/api/plugins")
def _rt_plugins(h):
    return h._json({"plugins": _plugins.list_plugins()})


@_routes.ROUTER.post("/api/skill-proposals")
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
    pid, why = _skills.proposal_add(inst["name"], b.get("name", ""), b.get("description", ""), b.get("content", ""),
                            turn=b.get("turn", ""), note=b.get("note", ""))
    if pid is None:
        return h._json({"error": why}, 400)
    return h._json({"id": pid, "msg": "proposed — waiting for approval in the Skills tab"})


@_routes.ROUTER.post("/api/sessions-search")
def _rt_sessions_search(h):
    # Guests search their own sessions; the orchestrator sees every
    # instance's (it delegates across them); admins may pass instance.
    b = h._body()
    inst = h._guest()
    if inst is not None:
        if not _util.rate_ok(("sessions-search", inst["name"]), 60, 300):
            return h._json({"error": "rate limit"}, 429)
        scope = None if inst["name"] == _guests.ORCH_INSTANCE else inst["name"]
        if scope is None and b.get("instance"):
            scope = str(b.get("instance"))[:80]
    else:
        scope = str(b.get("instance") or "")[:80] or None
    try:
        limit = max(1, min(int(b.get("limit", 10)), 50))
    except (TypeError, ValueError):
        limit = 10
    return h._json({"hits": _skills.sessions_search(str(b.get("q") or b.get("query") or ""), instance=scope, limit=limit)})


@_routes._msg_route("POST", "/api/ha-alias", admin=False)
def _rt_ha_alias(h):
    # Guest teaches HA a spoken-name alias; the HA token stays on the host.
    g = h._guest()
    if g is not None and not _policy.tool_allowed(g, "ha_learn_alias"):
        return "ha_learn_alias not allowed for this instance"
    b = h._body()
    return _haalias.learn_alias(b.get("spoken", ""), b.get("entity", ""))


@_routes._msg_route("POST", "/api/ha-control", admin=False)
def _rt_ha_control(h):
    # Deterministic voice control: matched server-side, no LLM in the loop.
    g = h._guest()
    if g is not None and not _policy.tool_allowed(g, "ha_control"):
        return "ha_control not allowed for this instance"
    b = h._body()
    return _haalias.control(b.get("spoken", ""), b.get("action", ""))


@_routes.ROUTER.get("/api/mounts")
def _rt_mounts(h):
    # The guest's reconciler fetches ITS list here, identified by source IP,
    # instead of reading .fcmnt/<name>/desired.list off the shared workspace
    # where any other VM could write it (and have this one mount a folder
    # over /bin). Admins may ask for an instance's list with ?instance=.
    inst = h._guest()
    if inst is None:
        name = _routes._qs(h).get("instance", [""])[0]
        inst = next((i for i in _instances.load_instances() if i["name"] == name), None)
        if inst is None:
            return h._json({"error": "instance?"}, 404)
    return _mounts.desired_lines(inst).encode(), "text/plain; charset=utf-8"
