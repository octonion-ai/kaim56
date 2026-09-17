# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""What the manager learns about the host it runs on: the uplink interface, the listen address, the time zone.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import os
import subprocess

from mgr import settings as _settings


def _uplink_iface():
    """Interface of the default route ("… dev eth0 …")."""
    try:
        out = subprocess.run(["ip", "-o", "route", "show", "default"],
                             capture_output=True, text=True, timeout=5).stdout.split()
        return out[out.index("dev") + 1]
    except Exception:
        return ""


def _pick_hostif():
    """Uplink for the guests' MASQUERADE rule. A hard-wired NIC name is a silent
    trap: if the kernel renames it (update, new hardware, reboot), the NAT rule
    points nowhere — the microVMs then reach neither DNS nor the LLM, and nothing
    logs an error. That's why a configured name only counts if the interface
    really exists; otherwise the default route wins."""
    want = os.environ.get("HOSTIF") or _settings.SITE.get("HOSTIF") or ""
    if want and os.path.exists(f"/sys/class/net/{want}"):
        return want
    auto = _uplink_iface()
    if want and auto:
        print(f"[net] HOSTIF={want} does not exist — using {auto} (default route)",
              flush=True)
    return auto or want or "eth0"


HOSTIF = _pick_hostif()
LISTEN = ("0.0.0.0", int(os.environ.get("PORT", "8700")))
def _host_tz():
    """The host's timezone name for the guests (they boot in UTC otherwise):
    /etc/timezone, else the /etc/localtime symlink, else UTC."""
    try:
        t = open("/etc/timezone").read().strip()
        if t:
            return t
    except OSError:
        pass
    try:
        p = os.path.realpath("/etc/localtime")
        if "/zoneinfo/" in p:
            return p.split("/zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"


HOST_TZ = os.environ.get("GUEST_TZ") or _host_tz()
