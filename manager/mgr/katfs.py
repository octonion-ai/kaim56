# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""katfs: P2P folder sharing (iroh) — proxy helpers to the host node.

Part of the mgr package; talks only to the loopback-bound katfs node.
"""
import io
import json
import os
import urllib.parse
import urllib.request
import zipfile

# ---- katfs (P2P folder sharing from the browser) --------------------------
# The katfs host node runs on the host (port 8790) and holds the iroh
# connection to the sharing browser tab. Agents reach <gateway>:8790 via
# remote_ls/remote_read/remote_write — none of it is mounted into the VM,
# katfs is not a filesystem.
#
# The manager proxies the share page under /katfs/: same origin, same auth —
# and above all HTTPS, which the browser's File System Access API strictly
# requires (secure context). That removes the SSH tunnel
# or the dedicated Traefik route from iroh-fs/README.md.
KATFS_HOST = os.environ.get("KATFS_HOST", "127.0.0.1")
KATFS_PORT = int(os.environ.get("KATFS_PORT", "8790"))
KATFS_BASE = f"http://{KATFS_HOST}:{KATFS_PORT}"


KATFS_MAX_WRITE = 64 * 1024 * 1024   # cap against disk DoS in the operator folder


def katfs_share_for(inst):
    """The share this instance MAY use. Exactly the one from its config —
    never one supplied by the guest. Empty means: the node decides, which it
    can only do while at most one share is active."""
    return (inst.get("config", {}).get("KATFS_SHARE", "") or "").strip()


def katfs_proxy_fs(op, share, path, recursive=False, body=None):
    """Forward a file operation to the (now loopback-bound) node. The caller
    has already verified the instance by source IP and set the share from its
    config — the guest cannot address a foreign share."""
    q = f"?path={urllib.parse.quote(path)}"
    if share:
        q += f"&share={urllib.parse.quote(share)}"
    if op == "delete" and recursive:
        q += "&recursive=1"
    method = "POST" if op in ("write", "delete") else "GET"
    req = urllib.request.Request(KATFS_BASE + "/" + op + q,
                                 data=(body if op == "write" else b"") if method == "POST" else None,
                                 method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.headers.get("Content-Type", "application/octet-stream"), r.read()


# Limits for the "download everything" ZIP: the node reads each file fully
# into memory, hence a cap against accidental giant shares.
KATFS_ZIP_MAX_FILES = 2000
KATFS_ZIP_MAX_BYTES = 512 * 1024 * 1024   # 512 MB gesamt


def katfs_zip(share, root):
    """Recursively collect the subtree from `root` of a share and return it as
    a ZIP. Runs over the same ls/read proxy calls as the browser,
    i.e. only while the share is open in the browser tab. Raises on trees that
    are too large, before it blows up memory."""
    root = (root or ".").strip() or "."
    buf = io.BytesIO()
    stats = {"files": 0, "bytes": 0}
    base = "" if root in (".", "") else root.rstrip("/")

    def walk(rel):
        st, _ct, data = katfs_proxy_fs("ls", share, rel or ".")
        if st != 200:
            raise RuntimeError(f"list {rel or '.'} -> {st}")
        for e in (json.loads(data or b"{}").get("entries") or []):
            name = e.get("name", "")
            if not name or name in (".", ".."):
                continue
            child = f"{rel}/{name}" if rel and rel != "." else name
            if e.get("dir"):
                walk(child)
                continue
            stats["files"] += 1
            if stats["files"] > KATFS_ZIP_MAX_FILES:
                raise RuntimeError(f"too many files (>{KATFS_ZIP_MAX_FILES})")
            fst, _fct, fdata = katfs_proxy_fs("read", share, child)
            if fst != 200:
                continue   # unlesbare Einzeldatei ueberspringen, Rest liefern
            stats["bytes"] += len(fdata)
            if stats["bytes"] > KATFS_ZIP_MAX_BYTES:
                raise RuntimeError("archive too large (>512 MB)")
            # path in the archive relative to the selected folder.
            arc = child[len(base) + 1:] if base and child.startswith(base + "/") else child
            zf.writestr(arc, fdata)

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        walk(base if base else ".")
    return buf.getvalue(), stats


def katfs_status():
    out = {"up": False, "connected": False, "share": "", "node_id": "",
           "port": KATFS_PORT, "error": "", "shares": []}
    try:
        with urllib.request.urlopen(KATFS_BASE + "/status", timeout=3) as r:
            out.update(json.loads(r.read().decode()))
        out["up"] = True
    except Exception as e:
        out["error"] = f"{e}"
        return out
    try:
        with urllib.request.urlopen(KATFS_BASE + "/nodeid", timeout=3) as r:
            out["node_id"] = json.loads(r.read().decode()).get("node_id", "")
    except Exception:
        pass
    # /shares only exists from the multi-share node on; an older one answers 404.
    try:
        with urllib.request.urlopen(KATFS_BASE + "/shares", timeout=3) as r:
            out["shares"] = json.loads(r.read().decode()).get("shares", [])
    except Exception:
        out["shares"] = []
    return out


def _katfs_answer(h, op, share, path, *extra):
    try:
        st, ct, data = katfs_proxy_fs(op, share, path, *extra)
    except urllib.error.HTTPError as e:
        st, ct, data = e.code, "application/json", e.read()
    except Exception as e:
        st, ct, data = 503, "application/json", json.dumps({"error": str(e)}).encode()
    return st, ct, data
