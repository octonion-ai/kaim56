#!/usr/bin/env python3
# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""kAIm56 — Manager (web UI + API) for 1..x microVM instances (Firecracker).

Runs as root (needs /dev/kvm, ip, iptables) under systemd. Pure standard
library. This file is the composition root and nothing else: it imports the
mgr package, wires the few cross-references the modules cannot derive
themselves, and starts the worker threads and the HTTP server. Everything
else lives in mgr/, one concern per module:

  security boundaries
    auth          admin login, lockout after failed logins, trusted Origins
    guests        a VM is identified by its source IP; what a guest may reach
    secrets       the host secret store and the per-instance release policy
    policy        tool catalog, per-instance tool gating, sandbox config
    llmproxy      the key proxy: LLM keys never enter a VM; budget/rate guard
    guestproxy    admin-to-VM relays: chat stream, terminal, katfs, /i/ proxy
    netfw         taps, anti-spoof, host input rules, egress per instance
    mounts        guest squash user, per-instance NFS exports, mount deny list
    routes_guest  every HTTP route a VM may call
    routes_admin  every HTTP route only the admin may call
  the VMs
    instances     instances/<name>.json, templates, network, running state
    vm            images, harness drive, write layer, start and stop
    tasks         task queue and worker, ephemeral VMs, orchestrator ping
    guestchat     talking to a VM's web bridge
    resources     CPU, memory and disk per instance
  HTTP
    routes        the route table (ROUTER), route helpers, body caps
    httpd         the handler class H: auth, guest gating, dispatch
    ui            the admin page (render) with ui_css / ui_html / ui_js
  data and integrations
    settings, about, models, chats, skills, personas, plugins, audit, voice,
    browse, store, memfs, hindsight, missions, rules, mcp, katfs, irohgw,
    signal, gateway, notify, saddler, websearch, extract, haalias
  small
    paths, host, util, startup

Conventions: an mgr module never imports this file. A module uses a sibling
as ``_name.func`` (module attribute, not ``from mgr.x import f``), so a test
can replace one definition in one place; the tests reach every module as
``manager._name`` — that is why every module is imported here, used or not.
"""
import os
import threading
from http.server import ThreadingHTTPServer

from mgr import about as _about              # noqa: F401
from mgr import audit as _audit
from mgr import auth as _auth
from mgr import browse as _browse            # noqa: F401
from mgr import chats as _chats              # noqa: F401
from mgr import gateway as _gateway
from mgr import guestchat as _guestchat      # noqa: F401
from mgr import guestproxy as _guestproxy    # noqa: F401
from mgr import guests as _guests            # noqa: F401
from mgr import haalias as _haalias
from mgr import hindsight as _hindsight
from mgr import host as _host
from mgr import httpd as _httpd
from mgr import instances as _instances
from mgr import irohgw as _irohgw
from mgr import katfs as _katfs              # noqa: F401
from mgr import llmproxy as _llmproxy        # noqa: F401
from mgr import mcp as _mcp
from mgr import memfs as _memfs
from mgr import missions as _missions
from mgr import models as _models            # noqa: F401
from mgr import mounts as _mounts
from mgr import netfw as _netfw              # noqa: F401
from mgr import notify as _notify
from mgr import paths as _paths
from mgr import personas as _personas        # noqa: F401
from mgr import plugins as _plugins          # noqa: F401
from mgr import policy as _policy            # noqa: F401
from mgr import resources as _resources      # noqa: F401
from mgr import routes as _routes            # noqa: F401
from mgr import routes_admin as _routes_admin  # noqa: F401  (registers its routes in ROUTER on import)
from mgr import routes_guest as _routes_guest  # noqa: F401  (registers its routes in ROUTER on import)
from mgr import rules as _rules
from mgr import saddler as _saddler_mod
from mgr import secrets as _secrets
from mgr import settings as _settings
from mgr import signal as _signal_mod
from mgr import skills as _skills            # noqa: F401
from mgr import startup as _startup
from mgr import store as _store
from mgr import tasks as _tasks
from mgr import ui as _ui                    # noqa: F401
from mgr import util as _util                # noqa: F401
from mgr import vm as _vm                    # noqa: F401
from mgr import voice as _voice              # noqa: F401
from mgr import websearch as _websearch_mod

# ---- wiring: what a module cannot derive itself ------------------------------
_missions.configure(_paths.BASE)
_mcp.configure(_paths.BASE, _instances.load_instances, _secrets.allowed_secret_keys, _secrets.secret_store)
_memfs.configure(_paths.BASE)
_hindsight.configure(lambda: _settings.load_settings(), log=print)
_signal_mod.configure(_paths.BASE)
_mcp.HUB_TZ = _host.HOST_TZ          # hub processes (caldav-mcp …) format dates in this zone
os.makedirs(_paths.RUN_DIR, exist_ok=True)
_gateway.configure(_paths.BASE)
_notify.configure(_paths.BASE)
_missions.notify_add = _notify.notify_add   # injection (mgr/missions)
_store.configure(_paths.BASE)
_missions.sem_store = _store.sem_store   # injection (mgr/missions)
_rules.configure(_paths.BASE)
_irohgw.configure(_paths.BASE)
_saddler_mod.configure(_audit.AUDIT_DIR, _store.HISTORY_DB)
_websearch_mod.configure(lambda key: (_settings.load_settings().get(key) or ""))


def _ha_ws_target():
    """(host, port) of the Home Assistant WebSocket, from the MCP catalog: the
    'homeassistant' entry carries the URL in args[0]; the WS address derives
    from it (no second place to configure)."""
    for m in _mcp.load_mcps():
        if m.get("name") == "homeassistant":
            for a in m.get("args", []):
                a = str(a)
                if a.startswith(("http://", "https://")):
                    hostport = a.split("//", 1)[1].split("/", 1)[0]
                    host, _, port = hostport.partition(":")
                    return host, int(port or "8123")
    return None


_haalias.configure(_ha_ws_target, lambda: _secrets.secret_store().get("HA_TOKEN"))

if __name__ == "__main__":
    print(f"kAIm56 on http://{_host.LISTEN[0]}:{_host.LISTEN[1]}  (auth={'on' if _auth.PW else 'OFF'})",
          flush=True)
    os.umask(0o077)                  # new files are root's; the few others read get a mode below
    _startup.harden_files()
    if _mounts.ensure_guest_user():
        _memfs.OWNER = (_mounts.GUEST_UID, _host.ADMIN_GID)
        _mounts.own_guest_dir(_mounts.AGENT_ROOT, 0o755)
        _mounts.own_guest_dir(_mounts.FCMNT_ROOT, 0o755)
    _mounts.retire_root_export()
    _startup.migrate_secrets_out_of_instances()
    _startup.migrate_mcp_config_out_of_instances()
    threading.Thread(target=_tasks._task_worker, daemon=True).start()
    threading.Thread(target=_signal_mod._signal_receiver, daemon=True).start()
    ThreadingHTTPServer(_host.LISTEN, _httpd.H).serve_forever()
