# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""What the agent reports about its own work: the audit line per tool call (what was touched, outcome, duration) and the trace of a turn (turn, LLM calls, tool calls as spans), both posted to the manager.

Part of the openrouter agent package (runs inside the VM): no import from the package root. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import time

from . import mgrclient as _mgrclient


def _audit_target(name, args):
    """The most meaningful field per tool for the audit log — never a secret value.
    For get_secret only the name, for write NOT the content."""
    a = args or {}
    if name in ("http_fetch",):
        return a.get("url", "")
    if name == "web_search":
        return a.get("query", "")
    if name in ("read_file", "write_file", "list_dir", "read_pdf",
                "remote_ls", "remote_read", "remote_write", "remote_delete"):
        return a.get("path", "")
    if name == "bash":
        return (a.get("command", "") or "")[:200]
    if name in ("get_secret", "load_skill", "memory_store", "memory_recall", "recall_tasks", "read_inbox"):
        return a.get("name", "") or a.get("key", "") or a.get("query", "")
    if name == "spawn_subagent":
        return (a.get("task", "") or "")[:120]
    if name == "create_task":
        return (a.get("target","") + ": " + (a.get("task","") or ""))[:160]
    return ""


# One id per user turn: lets a trace reviewer group the tool calls of a turn
# and line them up with the chat log. Set in run()/run_stream().
_turn_id = [""]


def audit(name, args, ok=True, err="", result="", ms=None):
    """Log a tool call at the manager (per instance, on the host — survives VM
    restarts). Best-effort: if the broker fails, the agent continues normally.
    Carries tool, target (URL/path/query), ok, a short ERROR TEXT and a short
    RESULT excerpt — without those a reviewer cannot tell a healthy call from
    one that failed politely (the audit used to say ok:true while a tool
    returned "⚠️ blocked"). NEVER secret values or full file contents."""
    try:
        rec = {"tool": name, "target": _audit_target(name, args), "ok": bool(ok),
               "err": str(err)[:300], "result": str(result)[:300],
               "turn": _turn_id[0]}
        if ms is not None:
            rec["ms"] = int(ms)
        _mgrclient._mgr(_mgrclient._manager_base(), "/api/audit", rec, timeout=5)
    except Exception:
        pass


# ---- traces: one turn = one span tree (turn -> LLM calls -> tool calls) -----
# The manager stitches them together from three feeds it already had: the
# audit (tool calls), the usage (LLM calls) and — new — a turn marker. Each
# record carries the turn id and its duration; nothing else changes.
_turn_step = [0]        # LLM calls so far in this turn (the span index)
_turn_t0 = [0.0]        # monotonic start of the turn
_turn_kind = ["chat"]


def trace_turn(event, **kw):
    """POST /api/trace {turn, event:start|end, kind, steps, ms, outcome}.
    Best-effort like audit(): the manager being away must not touch a turn."""
    try:
        _mgrclient._mgr(_mgrclient._manager_base(), "/api/trace",
             {"turn": _turn_id[0], "event": event, "kind": _turn_kind[0], **kw}, timeout=5)
    except Exception:
        pass


def _trace_begin(kind):
    _turn_kind[0] = kind
    _turn_step[0] = 0
    _turn_t0[0] = time.monotonic()
    trace_turn("start")


def _trace_end(outcome):
    trace_turn("end", steps=_turn_step[0], ms=int((time.monotonic() - _turn_t0[0]) * 1000),
               outcome=outcome)
