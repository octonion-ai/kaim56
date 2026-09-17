# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Admin login (security boundary): the Basic-auth credentials, lockout after failed logins, and which browser Origins are trusted (CSRF guard).

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import os
import re
import subprocess
import threading
import time
import urllib.request

from mgr import settings as _settings


_trusted_cache = {"ts": 0.0, "hosts": set()}


def trusted_hosts():
    """Hosts a browser may present as Origin: the site's public name(s),
    localhost and the host's own addresses (refreshed every minute), plus
    site.json TRUSTED_HOSTS for a reverse proxy under another name."""
    now = time.time()
    if now - _trusted_cache["ts"] > 60:
        hosts = {_settings.PUBLIC_HOST, "localhost", "127.0.0.1", "::1"}
        hosts.update(str(x) for x in (_settings.SITE.get("TRUSTED_HOSTS") or []) if x)
        try:
            r = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=5)
            hosts.update(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", r.stdout))
        except (OSError, subprocess.SubprocessError):
            pass
        _trusted_cache.update(ts=now, hosts={h.lower() for h in hosts})
    return _trusted_cache["hosts"]


def origin_allowed(origin):
    """CSRF guard for state-changing requests. Browsers send Origin on every
    POST; the app, the desktop client and curl do not (empty = fine). A page
    on another site — or a DNS-rebound name — carries a foreign Origin and is
    refused, so it cannot export a folder into a VM or create an instance."""
    o = (origin or "").strip()
    if not o:
        return True
    try:
        host = (urllib.parse.urlsplit(o).hostname or "").lower()
    except ValueError:
        return False
    return bool(host) and host in trusted_hosts()

USER = os.environ.get("MANAGER_USER", "admin")
PW = os.environ.get("MANAGER_PASS", "")   # empty => no auth (only behind Traefik!)

# Failed logins per client: after AUTH_FAILS_MAX within AUTH_FAIL_WINDOW the
# client is refused for AUTH_LOCK seconds, right password or not.
AUTH_FAILS_MAX, AUTH_FAIL_WINDOW, AUTH_LOCK = 10, 900, 900
_auth_fails, _auth_lock = {}, threading.Lock()
_PROXY_PEERS = ("127.0.0.1", "::1", "172.17.")


def auth_client_key(peer, xff=""):
    """Who is knocking: the socket peer — or, when that is a proxy on this
    host (Traefik on loopback/docker), the first X-Forwarded-For hop."""
    if xff and (peer in _PROXY_PEERS or peer.startswith(_PROXY_PEERS[2])):
        return xff.split(",")[0].strip() or peer
    return peer


def auth_locked(key, now=None):
    now = now or time.time()
    with _auth_lock:
        fails = [t for t in _auth_fails.get(key, []) if now - t < AUTH_FAIL_WINDOW]
        _auth_fails[key] = fails
        return len(fails) >= AUTH_FAILS_MAX and now - fails[-1] < AUTH_LOCK


def auth_failed(key, now=None):
    """Record a failure; True when this one closed the door."""
    now = now or time.time()
    with _auth_lock:
        fails = [t for t in _auth_fails.get(key, []) if now - t < AUTH_FAIL_WINDOW]
        fails.append(now)
        _auth_fails[key] = fails
        return len(fails) == AUTH_FAILS_MAX


def auth_succeeded(key):
    with _auth_lock:
        _auth_fails.pop(key, None)
