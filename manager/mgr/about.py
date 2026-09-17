# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""What the About/Updates tab shows: installed version against the newest GitHub release, the update run, the changelog and the security checklist (security.json).

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.request

from mgr import mcp as _mcp
from mgr import paths as _paths
from mgr import settings as _settings


# ── Updates: the installed version (install.sh writes VERSION from `git
# describe`) against the newest GitHub release; the update itself is the
# installer again, run by the oneshot unit install.sh installs alongside.
VERSION_FILE = os.path.join(_paths.BASE, "VERSION")
UPDATE_REPO = _settings.SITE.get("UPDATE_REPO") or "uneidel/kaim56"
UPDATE_UNIT = "kaim56-update.service"
UPDATE_LOG = os.path.join(_paths.RUN_DIR, "update.log")
_update = {"ts": 0.0, "latest": "", "url": "", "notes": "", "error": ""}
_update_lock = threading.Lock()


def installed_version():
    try:
        with open(VERSION_FILE) as fh:
            return fh.read().strip() or "dev"
    except OSError:
        return "dev"


def _fetch_latest_release():
    req = urllib.request.Request(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "kaim56"})
    d = json.loads(urllib.request.urlopen(req, timeout=6).read().decode())
    return {"latest": str(d.get("tag_name") or ""), "url": str(d.get("html_url") or ""),
            "notes": str(d.get("body") or "")[:1200]}


def update_check(force=False):
    """The newest release, cached for six hours (ten minutes after a failed
    check). UPDATE_CHECK=0 keeps the manager from calling GitHub at all."""
    if os.environ.get("UPDATE_CHECK", "1") in ("0", "false", "False"):
        return dict(_update)
    with _update_lock:
        age = time.time() - _update["ts"]
        if not force and age < (600 if _update["error"] else 21600):
            return dict(_update)
        try:
            _update.update(_fetch_latest_release(), error="")
        except Exception as e:
            _update["error"] = f"{e.__class__.__name__}: {e}"[:200]
        _update["ts"] = time.time()
        return dict(_update)


def _ver_key(v):
    """v1.2.3 / 1.2.3 / v1.2.3-4-gabc → (1, 2, 3); None for anything else (dev)."""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", str(v or ""))
    return tuple(int(x) for x in m.groups()) if m else None


def update_available(installed, latest):
    a, b = _ver_key(installed), _ver_key(latest)
    return bool(a and b and b > a)


def update_status():
    unit = os.path.exists(os.path.join("/etc/systemd/system", UPDATE_UNIT))
    active = ""
    if unit:
        try:
            r = subprocess.run(["systemctl", "is-active", UPDATE_UNIT], capture_output=True, text=True, timeout=5)
            active = r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    log = ""
    try:
        with open(UPDATE_LOG, errors="replace") as fh:
            log = "".join(fh.readlines()[-15:])
    except OSError:
        pass
    return {"unit": unit, "updating": active in ("activating", "active"), "log": log}


def update_start():
    st = update_status()
    if not st["unit"]:
        return "update service not installed — run install.sh once more, it installs it"
    if st["updating"]:
        return "an update is already running"
    try:
        open(UPDATE_LOG, "w").close()
        r = subprocess.run(["systemctl", "start", "--no-block", UPDATE_UNIT], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return f"error: {e}"
    if r.returncode:
        return f"error: {r.stderr.strip() or r.returncode}"
    print(f"[update] started {UPDATE_UNIT}", flush=True)
    return "update started — the manager restarts when the installer is done"

CHANGELOG_FILE = os.path.join(_paths.BASE, "CHANGELOG.md")
SECURITY_FILE = os.path.join(_paths.BASE, "security.json")


_mcp.configure(_paths.BASE, _mcp.load_instances)   # injection (mgr/mcp)

def load_changelog():
    try:
        with open(CHANGELOG_FILE) as fh:
            return fh.read()
    except OSError:
        return "# Changelog\n\n(no entries yet)"


def load_security():
    try:
        with open(SECURITY_FILE) as fh:
            d = json.load(fh)
        items = d.get("issues") if isinstance(d, dict) else d
        return items if isinstance(items, list) else []
    except (FileNotFoundError, ValueError):
        return []


def save_security(items):
    """Only toggle the status — text and assessment come from the file; the UI
    must not be able to rewrite findings."""
    cur = {i.get("id"): i for i in load_security()}
    n = 0
    for upd in items if isinstance(items, list) else []:
        it = cur.get(upd.get("id"))
        if it and upd.get("status") in ("open", "done") and it.get("status") != upd["status"]:
            it["status"] = upd["status"]
            n += 1
    with open(SECURITY_FILE, "w") as fh:
        json.dump({"issues": list(cur.values())}, fh, indent=2, ensure_ascii=False)
    return f"{n} entry/entries updated"
