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
import base64
import codecs
import json
import mimetypes
import os
import re
import hmac
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import chatui   # chat interface (/chat), lives next to this file

from mgr import paths as _paths  # noqa: E402
from mgr import tasks as _tasks  # noqa: E402
from mgr import guestchat as _guestchat  # noqa: E402
from mgr import resources as _resources  # noqa: E402
from mgr import personas as _personas  # noqa: E402
from mgr import policy as _policy  # noqa: E402
from mgr import skills as _skills  # noqa: E402
from mgr import chats as _chats  # noqa: E402
from mgr import netfw as _netfw  # noqa: E402,F401  (tests reach it as m._x)
from mgr import mounts as _mounts  # noqa: E402
from mgr import vm as _vm  # noqa: E402
from mgr import guests as _guests  # noqa: E402
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
_mcp.configure(_paths.BASE, _instances.load_instances, _secrets.allowed_secret_keys, _secrets.secret_store)
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
# Request bodies are read whole into the root process: cap them. A guest (or
# anyone on the LAN) must not be able to hand the manager a gigabyte.
BODY_MAX = 4 * 1024 * 1024            # JSON routes
BODY_MAX_LLM = 8 * 1024 * 1024        # chat completions (long contexts, images)
BODY_MAX_AUDIO = 32 * 1024 * 1024     # STT uploads


_mcp.HUB_TZ = _host.HOST_TZ          # hub processes (caldav-mcp …) format dates in this zone
os.makedirs(_paths.RUN_DIR, exist_ok=True)


# ---- Signal (send/HITL/receive): moved out to mgr/signal.py ---------------


# ---- Security gateway: moved out to mgr/gateway.py -------------------------
from mgr import gateway as _gateway  # noqa: E402
_gateway.configure(_paths.BASE)


# ---- Notifications: moved out to mgr/notify.py -----------------------------
from mgr import notify as _notify  # noqa: E402
_notify.configure(_paths.BASE)
_missions.notify_add = _notify.notify_add   # injection (mgr/missions)


# ---- Background jobs (task queue + scheduler) ------------------------------

# ---- store: SQLite history/usage/semantics + memory -> mgr/store.py -------
from mgr import store as _store  # noqa: E402
_store.configure(_paths.BASE)
_missions.sem_store = _store.sem_store   # injection (mgr/missions)


# ---- Playbooks + prompt templates: moved out to mgr/rules.py ---------------
from mgr import rules as _rules  # noqa: E402
_rules.configure(_paths.BASE)


# ---- Missions: moved out to mgr/missions.py (imported early, see above) ----


# ---- MCP catalog + hub: moved out to mgr/mcp.py ----------------------------


# ---- katfs: moved out to mgr/katfs.py --------------------------------------

from mgr import irohgw as _irohgw  # noqa: E402
_irohgw.configure(_paths.BASE)

# ---- web -------------------------------------------------------------------
# PAGE (HTML/JS of the manager UI) now lives in mgr/ui.py.

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
    return (json.dumps([{**i, "running": _instances.is_running(i), "stale": _vm.image_state(i)[0]}
                        for i in _instances.load_instances()]).encode(),
            "application/json")


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
    return h._json(_instances.session_info(inst))


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
    return json.dumps(_personas.load_personas(), ensure_ascii=False).encode(), "application/json"


@ROUTER.get("/api/skills")
def _rt_skills(h):
    # ?meta=1: name + description only. The full catalog is ~870 KB with the
    # bodies — the agents call this on every list_skills and never need them.
    q = urllib.parse.parse_qs(h.path.partition("?")[2])
    items = _skills.load_skills()
    if q.get("meta", ["0"])[0] == "1":
        items = [{"name": x.get("name", ""), "description": x.get("description", "")}
                 for x in items]
    return json.dumps(items, ensure_ascii=False).encode(), "application/json"


@ROUTER.get("/api/skills/", prefix=True)
def _rt_skill(h):
    nm = re.sub(r"[^a-z0-9_-]", "", h.path.split("/api/skills/", 1)[1].lower())
    sk = next((x for x in _skills.load_skills() if x.get("name") == nm), None)
    return ((sk.get("content", "") if sk else f"Skill '{nm}' not found").encode(),
            "text/plain; charset=utf-8")


@ROUTER.get("/api/saddler")
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


@ROUTER.get("/api/websearch")
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
    return json.dumps({"resources": _resources.resource_stats()}).encode(), "application/json"


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
    return _ui.LOGO_SVG.encode(), "image/svg+xml"


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
        if not _auth.PW or _guests.instance_by_ip(self.client_address[0]) is not None:
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
        if not _guestchat.wait_web(inst):
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
            _guestchat.guest_stream(inst, msg, img, emit)
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
                _chats.voice_session(name, src, chat_id or "", reset=True)
            elif text and not text.startswith("/"):
                sid = _chats.voice_session(name, src, chat_id or "")
                if sid:
                    _chats.chat_log_append(name, sid, text, "".join(parts).strip(), kind="voice")
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
        if _guests.instance_by_ip(self.client_address[0]) is not None:
            return self._forbid()
        if self.path.split("?", 1)[0].startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        body = _ui.render().encode()
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
        ok_g, why = _llmproxy._guard_check(_guests.instance_by_ip(self.client_address[0]))
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
        ginst = _guests.instance_by_ip(self.client_address[0])
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
        guest = _guests.instance_by_ip(self.client_address[0])
        if guest is None and not _auth.origin_allowed(self.headers.get("Origin", "")):
            return self._json({"error": "cross-site request refused"}, 403)
        if guest is not None and not (
                p in _guests.GUEST_POST_PATHS or p.startswith(_guests.GUEST_POST_PREFIXES)):
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
        if admin_only and _guests.instance_by_ip(self.client_address[0]) is not None:
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
        return _guests.instance_by_ip(self.client_address[0])


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
    body = chatui.render(_guestchat.web_instances(), want, _ui.LOGO_INLINE).encode()
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
    return h._json({"tools": _policy.AGENT_TOOLS_CATALOG})


@ROUTER.get("/api/agents")
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


@ROUTER.get("/api/inbox")
def _rt_inbox(h):
    # EVERY user message of every chat — among guests only the orchestrator.
    guest = h._guest()
    if guest is not None and guest["name"] != _guests.ORCH_INSTANCE:
        return h._forbid()
    peek = _qs(h).get("peek", ["0"])[0] == "1"
    return h._json({"messages": _chats.inbox_since(peek=peek)})


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
    if g is not None and g.get("name") != _guests.ORCH_INSTANCE:
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
    scope = guest["name"] if guest is not None and guest["name"] != _guests.ORCH_INSTANCE else None
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
    if inst is not None and not _policy.tool_allowed(inst, "send_signal"):
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
            _chats.chat_log_append(inst["name"], body.get("sender", ""), body.get("user", ""), body.get("reply", ""))
        except Exception as e:
            print(f"[quiet] chat_log_append failed: {e!r}", flush=True)
        try:
            _tasks.orchestrator_ping()   # Signal message -> orchestrator immediately
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
    return h._json({"instances": [_policy.effective_policy(i) for i in _instances.load_instances()]})


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
        rev, chats = _chats.wait_chats(*_since_wait(q))
        return h._json({"rev": rev, "chats": chats, "tombstones": _chats.load_tombstones()})
    return h._json(_chats.load_chats())


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
    n = _chats.merge_chats(h._body(default=[]))
    try:
        _tasks.orchestrator_ping()   # new app/web message -> orchestrator immediately
    except Exception:
        pass
    return f"{n} chats saved" if n >= 0 else "error while saving"


@_msg_route("POST", "/api/tasks")
def _rt_tasks_create(h):
    b = h._body()
    target, terr = _tasks.resolve_task_target(b.get("instance"))
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
        inst_new, terr = (_tasks.resolve_task_target(b.get("instance")) if b.get("instance") else (None, ""))
        return terr or _store.update_task(tid, b.get("message"), b.get("schedule"), instance=inst_new)
    if action == "run":
        return _tasks.run_task_now(tid)
    return "unknown"


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
    return _personas.upsert_persona(b.get("name", ""), b.get("prompt", ""), b.get("tools"), b.get("model"))


@_msg_route("POST", "/api/personas/", prefix=True)
def _rt_personas_delete(h):
    parts = h.path.split("?", 1)[0].strip("/").split("/")
    if len(parts) == 4 and parts[3] == "delete":
        return _personas.delete_persona(re.sub(r"[^a-z0-9_-]", "", parts[2].lower()))
    return "unknown"


@_msg_route("POST", "/api/skills")
def _rt_skills_upsert(h):
    b = h._body()
    return _skills.upsert_skill(b.get("name", ""), b.get("description", ""), b.get("content", ""))


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
    pid, why = _skills.proposal_add(inst["name"], b.get("name", ""), b.get("description", ""), b.get("content", ""),
                            turn=b.get("turn", ""), note=b.get("note", ""))
    if pid is None:
        return h._json({"error": why}, 400)
    return h._json({"id": pid, "msg": "proposed — waiting for approval in the Skills tab"})


@ROUTER.get("/api/skill-proposals", admin=True)
def _rt_skill_proposals(h):
    return h._json({"proposals": [p for p in _skills.load_proposals() if p.get("status") == "proposed"]})


@_msg_route("POST", "/api/skill-proposals/", prefix=True)
def _rt_skill_proposal_decide(h):
    parts = h.path.split("?", 1)[0].strip("/").split("/")
    if len(parts) != 4 or parts[3] not in ("approve", "discard"):
        return "unknown"
    return _skills.proposal_decide(re.sub(r"[^a-f0-9]", "", parts[2]), parts[3] == "approve")


@ROUTER.post("/api/sessions-search")
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


@_msg_route("POST", "/api/skills/", prefix=True)
def _rt_skills_delete(h):
    parts = h.path.split("?", 1)[0].strip("/").split("/")
    if len(parts) == 4 and parts[3] == "delete":
        return _skills.delete_skill(re.sub(r"[^a-z0-9_-]", "", parts[2].lower()))
    return "unknown"


@_msg_route("POST", "/api/ha-alias", admin=False)
def _rt_ha_alias(h):
    # Guest teaches HA a spoken-name alias; the HA token stays on the host.
    g = h._guest()
    if g is not None and not _policy.tool_allowed(g, "ha_learn_alias"):
        return "ha_learn_alias not allowed for this instance"
    b = h._body()
    return _haalias.learn_alias(b.get("spoken", ""), b.get("entity", ""))


@_msg_route("POST", "/api/ha-control", admin=False)
def _rt_ha_control(h):
    # Deterministic voice control: matched server-side, no LLM in the loop.
    g = h._guest()
    if g is not None and not _policy.tool_allowed(g, "ha_control"):
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
    tools = [t for t in (body.get("tools") or []) if t in _policy.AGENT_TOOL_NAMES]
    if tools and set(tools) != _policy.AGENT_TOOL_NAMES:
        cfg["AGENT_TOOLS"] = ",".join(tools)
    return _instances.create_instance(body.get("name", ""), body.get("template", ""), cfg,
                           body.get("mounts", []), internet=body.get("internet", True))


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
    return _mounts.desired_lines(inst).encode(), "text/plain; charset=utf-8"


@_msg_route("POST", "/api/instances/", prefix=True)
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
    if _mounts.ensure_guest_user():
        _memfs.OWNER = (_mounts.GUEST_UID, _host.ADMIN_GID)
        _mounts.own_guest_dir(_mounts.AGENT_ROOT, 0o755)
        _mounts.own_guest_dir(_mounts.FCMNT_ROOT, 0o755)
    _mounts.retire_root_export()
    migrate_secrets_out_of_instances()
    migrate_mcp_config_out_of_instances()
    threading.Thread(target=_tasks._task_worker, daemon=True).start()
    threading.Thread(target=_signal_mod._signal_receiver, daemon=True).start()
    ThreadingHTTPServer(_host.LISTEN, H).serve_forever()
