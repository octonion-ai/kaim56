# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Audit log per instance: what an agent's tools touched (tool calls, URLs, outcomes), appended by the guest reports and read by the Activity tab.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import json
import os
import time

from mgr import paths as _paths


# ---- Audit log per instance (tool calls, URLs) -----------------------------
# Lives on the host (survives VM restarts). JSONL, one file per instance,
# hard-capped to the last N lines.
AUDIT_DIR = os.path.join(_paths.BASE, "audit")
AUDIT_MAX_LINES = 2000


def audit_append(inst_name, tool, target, ok, err="", result="", turn="", ms=None):
    os.makedirs(AUDIT_DIR, exist_ok=True)
    p = os.path.join(AUDIT_DIR, f"{inst_name}.jsonl")
    rec = {"ts": int(time.time()), "tool": str(tool)[:64],
           "target": str(target)[:400], "ok": bool(ok)}
    # Rich fields (additive, old readers unaffected): the error text and a
    # result excerpt are what makes the trail reviewable — ok alone cannot
    # distinguish a healthy call from one that failed politely.
    if err:
        rec["err"] = str(err)[:300]
    if result:
        rec["result"] = str(result)[:300]
    if turn:
        rec["turn"] = str(turn)[:16]
    if ms is not None:
        try:
            rec["ms"] = int(ms)          # the span's duration (additive field)
        except (TypeError, ValueError):
            pass
    with open(p, "a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    # trim occasionally so the file doesn't grow without bound
    try:
        with open(p) as fh:
            lines = fh.readlines()
        if len(lines) > AUDIT_MAX_LINES + 200:
            with open(p, "w") as fh:
                fh.writelines(lines[-AUDIT_MAX_LINES:])
    except OSError as e:
        print(f"[quiet] audit trim for {inst_name} failed: {e!r}", flush=True)



def audit_read(inst_name, limit=200):
    p = os.path.join(AUDIT_DIR, f"{inst_name}.jsonl")
    try:
        with open(p) as fh:
            lines = fh.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return list(reversed(out))   # newest first
