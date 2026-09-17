# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""The UI's host-folder picker: which host directories the admin may browse when mounting a folder into a VM.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import os

from mgr import settings as _settings


# ---- Browse host folders (the UI's folder picker) --------------------------
# Directory names only, never file contents. The manager runs as root and thus
# sees everything — the route is admin-only like /api/secret-keys (guests
# blocked by source IP) and sits behind the same auth as the UI.

# The picker exists to choose folders for guest mounts — it has no business
# mapping /etc or /root. Admin auth still applies; this bounds what a stolen
# admin password can enumerate.
BROWSE_ROOTS = tuple((_settings.SITE.get("BROWSE_ROOTS") or ["/home", "/srv", "/mnt", "/media"]))


def list_dirs(path, show_hidden=False):
    p = os.path.abspath(path or "/") or "/"
    parent = "" if p == "/" else os.path.dirname(p)
    inside = any(p == r or p.startswith(r.rstrip("/") + "/") for r in BROWSE_ROOTS)
    if not inside:
        # Outside the allowed roots the picker shows the roots themselves —
        # that keeps "/" navigable without exposing the rest of the tree.
        roots = [r for r in BROWSE_ROOTS if os.path.isdir(r)]
        return {"path": "/", "parent": "", "dirs": [r.lstrip("/") for r in roots]}
    if not os.path.isdir(p):
        return {"path": p, "parent": parent, "dirs": [], "error": "not a directory"}
    try:
        dirs = sorted((e.name for e in os.scandir(p)
                       if e.is_dir(follow_symlinks=False)
                       and (show_hidden or not e.name.startswith("."))),
                      key=str.lower)
    except OSError as e:
        return {"path": p, "parent": parent, "dirs": [], "error": f"no access ({e.strerror})"}
    return {"path": p, "parent": parent, "dirs": dirs}
