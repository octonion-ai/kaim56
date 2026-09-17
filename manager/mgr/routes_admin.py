# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""HTTP routes for the admin only (admin=True, or the {"msg": …} family): guests get 403 from the dispatcher before any of these run. Guest-reachable routes live in mgr/routes_guest.py.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import base64
import chatui
import json
import mimetypes
import os
import re
import urllib.request

from mgr import about as _about
from mgr import audit as _audit
from mgr import browse as _browse
from mgr import chats as _chats
from mgr import gateway as _gateway
from mgr import guestchat as _guestchat
from mgr import instances as _instances
from mgr import irohgw as _irohgw
from mgr import katfs as _katfs
from mgr import mcp as _mcp
from mgr import missions as _missions
from mgr import models as _models
from mgr import mounts as _mounts
from mgr import notify as _notify
from mgr import paths as _paths
from mgr import personas as _personas
from mgr import plugins as _plugins
from mgr import policy as _policy
from mgr import resources as _resources
from mgr import routes as _routes
from mgr import rules as _rules
from mgr import secrets as _secrets
from mgr import settings as _settings
from mgr import skills as _skills
from mgr import store as _store
from mgr import tasks as _tasks
from mgr import ui as _ui
from mgr import util as _util
from mgr import vm as _vm
from mgr import voice as _voice


@_routes.ROUTER.get("/api/instances", admin=True)
def _rt_instances(h):
    return (json.dumps([{**i, "running": _instances.is_running(i), "stale": _vm.image_state(i)[0]}
                        for i in _instances.load_instances()]).encode(),
            "application/json")


@_routes.ROUTER.get("/api/session/", prefix=True, admin=True)
def _rt_session(h):
    # /api/session/<instance>        -> the session panel's data
    # /api/session/<instance>/log    -> the VM's console log tail (text)
    parts = _routes._tail(h, "/api/session/")
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
    return h._json(_instances.session_info(inst))


@_routes.ROUTER.get("/api/settings", admin=True)
def _rt_settings(h):
    return json.dumps(_settings.settings_for_ui()).encode(), "application/json"


# What TTS should NOT read: tool status lines ("🔧 ha_control …"), think blocks,
# code fences, markdown decor, URLs. The desktop client filters this itself;
# the web chat's "Read aloud", the app and the ESP client send the reply as
# is — so the manager filters once for everyone, right before Piper.
@_routes.ROUTER.get("/api/stt-recent", admin=True)
def _rt_stt_recent(h):
    return json.dumps({"recent": _voice.stt_recent()}, ensure_ascii=False).encode(), "application/json"


@_routes.ROUTER.get("/api/stt-recent/audio", admin=True)
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


@_routes.ROUTER.get("/api/tasks", admin=True)
def _rt_tasks(h):
    return json.dumps(_store.load_tasks()).encode(), "application/json"


@_routes.ROUTER.get("/api/usage", admin=True)
def _rt_usage(h):
    return json.dumps(_store.usage_summary()).encode(), "application/json"


@_routes.ROUTER.get("/api/usage-by-model", admin=True)
def _rt_usage_by_model(h):
    try:
        since = int(_routes._qs(h).get("since", ["0"])[0] or 0)
    except ValueError:
        since = 0
    return h._json({"rows": _store.usage_by_model(since)})


@_routes.ROUTER.get("/api/version", admin=True)
def _rt_version(h):
    u = _about.update_check(force="force" in _routes._qs(h))
    inst = _about.installed_version()
    return h._json({"installed": inst, "latest": u["latest"], "url": u["url"], "notes": u["notes"],
                    "error": u["error"], "available": _about.update_available(inst, u["latest"]), **_about.update_status()})


@_routes.ROUTER.get("/api/gateway", admin=True)
def _rt_gateway(h):
    g = _gateway.load_gateway()
    g["available"] = _gateway._clean_unicode is not None
    return json.dumps(g).encode(), "application/json"



@_routes.ROUTER.post("/api/extract", admin=True)
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



@_routes.ROUTER.get("/api/resources", admin=True)
def _rt_resources(h):
    return json.dumps({"resources": _resources.resource_stats()}).encode(), "application/json"



# ---- pages and proxies ------------------------------------------------------
@_routes.ROUTER.get("/chat", admin=True)
def _rt_chat_page(h):
    want = _routes._qs(h).get("i", [""])[0]
    body = chatui.render(_guestchat.web_instances(), want, _ui.LOGO_INLINE).encode()
    h.send_response(200)
    h.send_header("Content-Type", "text/html; charset=utf-8")
    # Don't cache: otherwise the browser holds on to an old version (that
    # was the cause of the gray emoji boxes after the icon fix).
    h.send_header("Cache-Control", "no-store, must-revalidate")
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


@_routes.ROUTER.get("/katfs", admin=True)
def _rt_katfs_redirect(h):
    h.send_response(301)
    h.send_header("Location", "/katfs/")
    h.end_headers()


@_routes.ROUTER.get("/katfs/", prefix=True, admin=True)
def _rt_katfs_proxy(h):
    return h._katfs_proxy()


@_routes.ROUTER.get("/i/", prefix=True, admin=True)
def _rt_instance_proxy_get(h):
    name, _, tail = h.path[3:].partition("/")
    if tail.split("?", 1)[0].rstrip("/").split("/")[0] == "term":
        return h._term_route(name, tail.split("?", 1)[0])
    return h._proxy("GET")


@_routes.ROUTER.post("/i/", prefix=True, admin=True)
def _rt_instance_proxy_post(h):
    return h._proxy("POST")


@_routes.ROUTER.post("/api/chat/", prefix=True, admin=True)
def _rt_chat_stream(h):
    return h._chat_stream(urllib.parse.unquote(h.path[len("/api/chat/"):].split("?", 1)[0]))



@_routes.ROUTER.post("/api/mission-admin", admin=True)
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



@_routes.ROUTER.get("/api/trace/", prefix=True, admin=True)
def _rt_trace_read(h):
    # One turn as a span tree: the turn row, its LLM calls (llm_usage) and its
    # tool calls (audit lines with that turn id), each with duration. Without
    # ?turn= the last 50 turns of the instance.
    nm = re.sub(r"[^a-zA-Z0-9_-]", "", _routes._tail(h, "/api/trace/")[0])
    q = _routes._qs(h)
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



@_routes.ROUTER.post("/api/notifications/read", admin=True)
def _rt_notifications_read(h):
    body = h._body()
    n = _notify.notif_clear() if body.get("clear") else _notify.notif_mark_read(body.get("id"), bool(body.get("all")))
    return h._json({"marked": n})



@_routes.ROUTER.get("/api/katfs/zip", admin=True)
def _rt_katfs_zip(h):
    q = _routes._qs(h)
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


@_routes.ROUTER.get("/api/katfs/browse", admin=True)
@_routes.ROUTER.get("/api/katfs/file", admin=True)
def _rt_katfs_browse(h):
    q = _routes._qs(h)
    path, share = q.get("path", ["."])[0], q.get("share", [""])[0]
    op = "ls" if "/browse" in h.path else "read"
    st, ct, data = _katfs._katfs_answer(h, op, share, path)
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


@_routes.ROUTER.get("/api/katfs/status", admin=True)
def _rt_katfs_status(h):
    return h._json(_katfs.katfs_status())


@_routes.ROUTER.get("/api/browse", admin=True)
def _rt_host_browse(h):
    q = _routes._qs(h)
    return h._json(_browse.list_dirs(q.get("path", ["/"])[0], q.get("hidden", [""])[0] == "1"))


# ---- admin reads --------------------------------------------------------------
@_routes.ROUTER.get("/api/usage/", prefix=True, admin=True)
def _rt_usage_for(h):
    nm = re.sub(r"[^a-zA-Z0-9_-]", "", _routes._tail(h, "/api/usage/")[0])
    try:
        since = int(_routes._qs(h).get("since", ["0"])[0] or 0)
    except ValueError:
        since = 0
    return h._json(_store.usage_for(nm, since))


@_routes.ROUTER.get("/api/policy", admin=True)
def _rt_policy(h):
    return h._json({"instances": [_policy.effective_policy(i) for i in _instances.load_instances()]})


@_routes.ROUTER.get("/api/audit/", prefix=True, admin=True)
def _rt_audit_read(h):
    nm = re.sub(r"[^a-zA-Z0-9_-]", "", _routes._tail(h, "/api/audit/")[0])
    return h._json({"instance": nm, "events": _audit.audit_read(nm, limit=1000)})



@_routes.ROUTER.get("/api/changelog", admin=True)
def _rt_changelog(h):
    return h._json({"text": _about.load_changelog()})


@_routes.ROUTER.get("/api/security", admin=True)
def _rt_security(h):
    return h._json({"issues": _about.load_security()})


@_routes.ROUTER.get("/api/secret-keys", admin=True)
def _rt_secret_keys(h):
    # Names only, never values; `sources` says where a key lives (the store
    # file, editable here, or the settings, edited in the Settings tab).
    store = _secrets.load_secrets_file()
    keys = sorted(_secrets.secret_store().keys())
    return h._json({"keys": keys, "sources": {k: ("store" if k in store else "settings") for k in keys}})


@_routes._msg_route("POST", "/api/update")
def _rt_update(h):
    return _about.update_start()


@_routes._msg_route("POST", "/api/secret-store")
def _rt_secret_store_set(h):
    b = h._body()
    return _secrets.secret_set(b.get("name", ""), b.get("value", ""))


@_routes._msg_route("POST", "/api/secret-store/", prefix=True)
def _rt_secret_store_delete(h):
    parts = h.path.split("?", 1)[0].strip("/").split("/")
    if len(parts) == 4 and parts[3] == "delete":
        return _secrets.secret_delete(parts[2])
    return "unknown"


@_routes.ROUTER.get("/api/secret-policy", admin=True)
def _rt_secret_policy(h):
    return h._json(_secrets.load_secret_policy())


@_routes.ROUTER.get("/api/mcps", admin=True)
def _rt_mcps(h):
    return h._json(_mcp.load_mcps())


@_routes.ROUTER.get("/api/openrouter-models", admin=True)
def _rt_openrouter_models(h):
    return h._json(_models.openrouter_models("refresh=1" in h.path, "tools=1" in h.path, "relevant=1" in h.path))


def _since_wait(q):
    try:
        since = int(q.get("since", ["0"])[0] or 0)
        wait = min(30.0, max(0.0, float(q.get("wait", ["25"])[0] or 0)))
    except ValueError:
        since, wait = 0, 0.0
    return since, wait


@_routes.ROUTER.get("/api/chats", admin=True)
def _rt_chats(h):
    q = _routes._qs(h)
    if "since" in q or "wait" in q:
        rev, chats = _chats.wait_chats(*_since_wait(q))
        return h._json({"rev": rev, "chats": chats, "tombstones": _chats.load_tombstones()})
    return h._json(_chats.load_chats())


@_routes.ROUTER.get("/api/notifications", admin=True)
def _rt_notifications(h):
    q = _routes._qs(h)
    if "since" in q or "wait" in q:
        rev, notifs = _notify.wait_notifs(*_since_wait(q))
        unread = sum(1 for n in _notify.load_notifications() if not n.get("read"))
        return h._json({"rev": rev, "notifications": notifs, "unread": unread})
    lst = _notify.load_notifications()
    return h._json({"notifications": lst, "unread": sum(1 for n in lst if not n.get("read"))})


# ---- admin writes: the {"msg": …} family ---------------------------------------
@_routes.ROUTER.post("/api/iroh", admin=True)
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


@_routes.ROUTER.post("/api/prompts", admin=True)
def _rt_prompts(h):
    b = h._body()
    msg = _rules.prompt_delete(b.get("name", "")) if b.get("delete") else _rules.prompt_upsert(b.get("name", ""), b.get("text", ""))
    return h._json({"msg": msg})


@_routes.ROUTER.post("/api/plugins", admin=True)
@_routes.ROUTER.post("/api/plugins/", prefix=True, admin=True)
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


@_routes._msg_route("POST", "/api/settings")
def _rt_settings_save(h):
    return _settings.save_settings(h._body())


@_routes._msg_route("POST", "/api/security")
def _rt_security_save(h):
    return _about.save_security(h._body().get("issues") or [])


@_routes._msg_route("POST", "/api/gateway")
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


@_routes._msg_route("POST", "/api/models")
def _rt_models_save(h):
    return _models.save_curated(h._body().get("curated") or [])


@_routes._msg_route("POST", "/api/chats")
def _rt_chats_merge(h):
    n = _chats.merge_chats(h._body(default=[]))
    try:
        _tasks.orchestrator_ping()   # new app/web message -> orchestrator immediately
    except Exception:
        pass
    return f"{n} chats saved" if n >= 0 else "error while saving"


@_routes._msg_route("POST", "/api/tasks")
def _rt_tasks_create(h):
    b = h._body()
    target, terr = _tasks.resolve_task_target(b.get("instance"))
    if not b.get("instance") or not b.get("message"):
        return "instance/message missing"
    if terr:
        return terr
    t = _store.add_task(target, b.get("message", ""), b.get("schedule", ""))
    return f"task {t['id']} created ({t['status']})"


@_routes._msg_route("POST", "/api/tasks/", prefix=True)
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
        inst_new, terr = (_tasks.resolve_task_target(b.get("instance")) if b.get("instance") else (None, ""))
        return terr or _store.update_task(tid, b.get("message"), b.get("schedule"), instance=inst_new)
    if action == "run":
        return _tasks.run_task_now(tid)
    return "unknown"


@_routes._msg_route("POST", "/api/secret-policy")
def _rt_secret_policy_save(h):
    return _secrets.save_secret_policy(h._body())


@_routes._msg_route("POST", "/api/mcps")
def _rt_mcps_upsert(h):
    b = h._body()
    return _mcp.upsert_mcp(b.get("name", ""), b.get("description", ""), b.get("command", ""), b.get("args", []), b.get("env"))


@_routes._msg_route("POST", "/api/mcps/", prefix=True)
def _rt_mcps_delete(h):
    parts = h.path.split("?", 1)[0].strip("/").split("/")
    if len(parts) == 4 and parts[3] == "delete":
        return _mcp.delete_mcp(re.sub(r"[^a-z0-9_-]", "", parts[2].lower()))
    return "unknown"


@_routes._msg_route("POST", "/api/personas")
def _rt_personas_upsert(h):
    b = h._body()
    return _personas.upsert_persona(b.get("name", ""), b.get("prompt", ""), b.get("tools"), b.get("model"))


@_routes._msg_route("POST", "/api/personas/", prefix=True)
def _rt_personas_delete(h):
    parts = h.path.split("?", 1)[0].strip("/").split("/")
    if len(parts) == 4 and parts[3] == "delete":
        return _personas.delete_persona(re.sub(r"[^a-z0-9_-]", "", parts[2].lower()))
    return "unknown"


@_routes._msg_route("POST", "/api/skills")
def _rt_skills_upsert(h):
    b = h._body()
    return _skills.upsert_skill(b.get("name", ""), b.get("description", ""), b.get("content", ""))



@_routes.ROUTER.get("/api/skill-proposals", admin=True)
def _rt_skill_proposals(h):
    return h._json({"proposals": [p for p in _skills.load_proposals() if p.get("status") == "proposed"]})


@_routes._msg_route("POST", "/api/skill-proposals/", prefix=True)
def _rt_skill_proposal_decide(h):
    parts = h.path.split("?", 1)[0].strip("/").split("/")
    if len(parts) != 4 or parts[3] not in ("approve", "discard"):
        return "unknown"
    return _skills.proposal_decide(re.sub(r"[^a-f0-9]", "", parts[2]), parts[3] == "approve")



@_routes._msg_route("POST", "/api/skills/", prefix=True)
def _rt_skills_delete(h):
    parts = h.path.split("?", 1)[0].strip("/").split("/")
    if len(parts) == 4 and parts[3] == "delete":
        return _skills.delete_skill(re.sub(r"[^a-z0-9_-]", "", parts[2].lower()))
    return "unknown"



@_routes._msg_route("POST", "/api/create")
def _rt_instance_create(h):
    body = h._body()
    cfg = body.get("config", {}) or {}
    mcps = [str(m) for m in (body.get("mcps") or []) if m]
    if mcps:
        cfg["MCP_SERVERS"] = ",".join(mcps)
    # Tool allowlist only as a real subset (all selected -> omit = all).
    tools = [t for t in (body.get("tools") or []) if t in _policy.AGENT_TOOL_NAMES]
    if tools and set(tools) != _policy.AGENT_TOOL_NAMES:
        cfg["AGENT_TOOLS"] = ",".join(tools)
    return _instances.create_instance(body.get("name", ""), body.get("template", ""), cfg,
                           body.get("mounts", []), internet=body.get("internet", True))



@_routes._msg_route("POST", "/api/instances/", prefix=True)
def _rt_instance_action(h):
    parts = h.path.split("?", 1)[0].strip("/").split("/")
    if len(parts) != 4:
        return "unknown"
    name, action = parts[2], parts[3]
    if action == "delete":
        return _instances.delete_instance(name)
    if action == "mounts":
        return _mounts.set_mounts(name, h._body().get("mounts", []))
    if action == "internet":
        return _instances.set_internet(name, bool(h._body().get("on", True)))
    if action == "tools":
        return _instances.set_instance_tools(name, h._body().get("tools") or [])
    if action == "config":
        b = h._body()
        return _instances._set_config_key(name, str(b.get("key", "")).strip(), b.get("value", ""))
    if action == "persist":
        return _vm.set_persist_disk(name, bool(h._body().get("on")))
    if action == "diskreset":
        return _vm.reset_upper(name)
    if action == "model":
        return _instances.set_model(name, h._body().get("model", ""))
    inst = next((i for i in _instances.load_instances() if i["name"] == name), None)
    if not inst:
        return "unknown"
    if action == "restart":       # stop/start: picks up a rebuilt image
        _vm.stop(inst)
        return _vm.start(inst)
    if action == "start":
        return _vm.start(inst)
    if action == "stop":
        return _vm.stop(inst)
    return "??"
