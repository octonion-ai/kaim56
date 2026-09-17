# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Small helpers shared by the manager modules: shell, the worker log, JSON inside a script block, safe download names, a sliding-window rate limiter, the body-size exception.

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

from mgr import paths as _paths


class BodyTooLarge(Exception):
    pass


def js_json(obj, **kw):
    """json.dumps for a value embedded in a <script> block: '</script>' inside
    a persona or an imported skill description must not end the block."""
    return (json.dumps(obj, **kw).replace("<", "\\u003c")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def download_name(name, default="file"):
    """A filename safe inside Content-Disposition (no quotes, no CR/LF)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or ""))[:120].strip("._") or default


_rate_hits, _rate_lock = {}, threading.Lock()


def rate_ok(key, limit, window):
    """Sliding window per key: True while fewer than `limit` hits in `window` s."""
    now = time.time()
    with _rate_lock:
        lst = _rate_hits.setdefault(key, [])
        lst[:] = [t for t in lst if now - t < window]
        if len(lst) >= limit:
            return False
        lst.append(now)
        return True

def sh(*args, check=True):
    return subprocess.run(args, capture_output=True, text=True, check=check)



WORKER_LOG = os.path.join(_paths.RUN_DIR, "worker.log")


def _wlog(msg):
    """Worker diagnostics into a file — journalctl is only accessible to root,
    and that is exactly why the exception was missing in the orphaned-task bug
    (Aug 20)."""
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)
    print("[worker]", msg, flush=True)
    try:
        with open(WORKER_LOG, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
