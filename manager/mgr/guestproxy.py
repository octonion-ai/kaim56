# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Admin-to-VM relays (security boundary): the chat token stream, the browser terminal (page served by the manager, only /ws tunneled — H-3 — with an Origin check on the handshake — H-2), the katfs browser and the generic /i/<name>/ proxy whose guest HTML is sandboxed. Mixed into mgr.httpd.H.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import codecs
import json
import re
import socket
import threading
import urllib.request

from mgr import auth as _auth
from mgr import chats as _chats
from mgr import gateway as _gateway
from mgr import guestchat as _guestchat
from mgr import instances as _instances
from mgr import katfs as _katfs


class GuestProxyMixin:
    """Handler methods (`self` is the HTTP handler) that relay the admin to a VM."""

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


_WS_KEEP = ("host", "upgrade", "connection", "origin", "user-agent", "pragma", "cache-control")


def ws_forward_headers(items):
    """The subset of a browser's upgrade headers the guest terminal gets."""
    return [(k, v) for k, v in items
            if k.lower() in _WS_KEEP or k.lower().startswith("sec-websocket-")]
