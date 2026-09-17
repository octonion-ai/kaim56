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
import json
import os
import hmac
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


from mgr import paths as _paths  # noqa: E402
from mgr import guestproxy as _guestproxy  # noqa: E402
# The route modules register their functions in mgr.routes.ROUTER when imported.
from mgr import routes_guest as _routes_guest  # noqa: E402,F401
from mgr import routes_admin as _routes_admin  # noqa: E402,F401
from mgr import tasks as _tasks  # noqa: E402
from mgr import guestchat as _guestchat  # noqa: E402,F401  (tests reach it as m._x)
from mgr import resources as _resources  # noqa: E402,F401  (tests reach it as m._x)
from mgr import personas as _personas  # noqa: E402,F401  (tests reach it as m._x)
from mgr import policy as _policy  # noqa: E402,F401  (tests reach it as m._x)
from mgr import skills as _skills  # noqa: E402,F401  (tests reach it as m._x)
from mgr import chats as _chats  # noqa: E402,F401  (tests reach it as m._x)
from mgr import netfw as _netfw  # noqa: E402,F401  (tests reach it as m._x)
from mgr import mounts as _mounts  # noqa: E402
from mgr import vm as _vm  # noqa: E402,F401  (tests reach it as m._x)
from mgr import guests as _guests  # noqa: E402
from mgr import instances as _instances  # noqa: E402
from mgr import secrets as _secrets  # noqa: E402
from mgr import llmproxy as _llmproxy  # noqa: E402,F401  (tests reach it as m._x)
from mgr import ui as _ui  # noqa: E402
from mgr import routes as _routes  # noqa: E402
from mgr import katfs as _katfs  # noqa: E402,F401  (tests reach it as m._x)
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

class H(_guestproxy.GuestProxyMixin, _llmproxy.LLMProxyMixin, BaseHTTPRequestHandler):
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
