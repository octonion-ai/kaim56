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


from mgr import paths as _paths  # noqa: E402
# The route modules register their functions in mgr.routes.ROUTER when imported.
from mgr import routes_guest as _routes_guest  # noqa: E402,F401
from mgr import routes_admin as _routes_admin  # noqa: E402,F401
from mgr import tasks as _tasks  # noqa: E402
from mgr import guestchat as _guestchat  # noqa: E402
from mgr import resources as _resources  # noqa: E402,F401  (tests reach it as m._x)
from mgr import personas as _personas  # noqa: E402,F401  (tests reach it as m._x)
from mgr import policy as _policy  # noqa: E402,F401  (tests reach it as m._x)
from mgr import skills as _skills  # noqa: E402,F401  (tests reach it as m._x)
from mgr import chats as _chats  # noqa: E402
from mgr import netfw as _netfw  # noqa: E402,F401  (tests reach it as m._x)
from mgr import mounts as _mounts  # noqa: E402
from mgr import vm as _vm  # noqa: E402,F401  (tests reach it as m._x)
from mgr import guests as _guests  # noqa: E402
from mgr import instances as _instances  # noqa: E402
from mgr import secrets as _secrets  # noqa: E402
from mgr import llmproxy as _llmproxy  # noqa: E402
from mgr import ui as _ui  # noqa: E402
from mgr import routes as _routes  # noqa: E402
from mgr import katfs as _katfs  # noqa: E402
from mgr import plugins as _plugins  # noqa: E402,F401  (tests reach it as m._x)
from mgr import voice as _voice  # noqa: E402,F401  (tests reach it as m._x)
from mgr import browse as _browse  # noqa: E402,F401  (tests reach it as m._x)
from mgr import audit as _audit  # noqa: E402
from mgr import auth as _auth  # noqa: E402
from mgr import host as _host  # noqa: E402
from mgr import util as _util  # noqa: E402
from mgr import models as _models  # noqa: E402,F401  (tests reach it as m._x)
from mgr import about as _about  # noqa: E402,F401  (tests reach it as m._x)
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
        payload = self._raw(_routes.BODY_MAX_LLM)
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
        hit = _routes.ROUTER.resolve(method, self.path)
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
        if ln > (limit or _routes.BODY_MAX):
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
