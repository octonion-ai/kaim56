# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""LLM key proxy (security boundary): the agents call /api/llm/<backend> and the manager adds the key, so no LLM key ever enters a VM; plus the per-instance budget and rate guard and the usage booking of proxied calls.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import json
import os
import threading
import time

from mgr import notify as _notify
from mgr import store as _store


# Credential injection gateway (OneCLI pattern): the agent sends its chat
# requests to /api/llm/<backend>/chat/completions instead of directly to the
# router; when forwarding, the manager appends the Authorization header from
# the settings. This way the LLM keys NEVER leave the host: a compromised VM
# can at most call models through the manager (visible, throttleable), but
# cannot exfiltrate a key and reuse it outside the system.
LLM_PROXY_UPSTREAMS = {
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY"),
    "orcarouter": ("https://api.orcarouter.ai/v1/chat/completions", "ORCAROUTER_API_KEY"),
}

# ---- Guardrails: budget + rate limit for LLM calls --------------------------
# Enforcement at the key injection proxy: all of the VMs' router calls pass
# through there. Budget per instance and day (tokens, from llm_usage) and a
# frequency cap per minute. Override per instance via config: BUDGET_TOKENS
# (0 = off), LLM_RATE_MIN. On exceedance: 429 + at most one notify per hour.
GUARD_BUDGET_TOKENS = int(os.environ.get("GUARD_BUDGET_TOKENS", "5000000"))
GUARD_LLM_RATE_MIN = int(os.environ.get("GUARD_LLM_RATE_MIN", "60"))
_guard_lock = threading.Lock()
_guard_calls = {}          # instance -> [timestamps]
_guard_notified = {}       # instance -> ts of the last budget notify


def _guard_check(inst):
    """(allowed, reason). inst = instance dict or None (admin/host: always ok)."""
    if inst is None:
        return True, ""
    name = inst["name"]
    cfg = inst.get("config") or {}
    now = time.time()
    # 1) Frequency per minute
    try:
        rate = int(cfg.get("LLM_RATE_MIN", GUARD_LLM_RATE_MIN))
    except ValueError:
        rate = GUARD_LLM_RATE_MIN
    with _guard_lock:
        lst = _guard_calls.setdefault(name, [])
        lst[:] = [t for t in lst if now - t < 60]
        if rate > 0 and len(lst) >= rate:
            return False, f"rate limit: {rate} LLM calls/min reached"
        lst.append(now)
    # 2) Daily budget (tokens since local midnight)
    try:
        budget = int(cfg.get("BUDGET_TOKENS", GUARD_BUDGET_TOKENS))
    except ValueError:
        budget = GUARD_BUDGET_TOKENS
    if budget > 0:
        midnight = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
        u = _store.usage_for(name, midnight)
        used = (u.get("in") or 0) + (u.get("out") or 0)
        if used >= budget:
            with _guard_lock:
                last = _guard_notified.get(name, 0)
                fire = now - last > 3600
                if fire:
                    _guard_notified[name] = now
            if fire:
                try:
                    _notify.notify_add("guardrail", f"Budget reached: {name}",
                               f"{used:,} tokens today (limit {budget:,}). LLM calls "
                               f"pause until midnight. Override: BUDGET_TOKENS in the "
                               f"instance config.", link="tasks")
                except Exception:
                    pass
            return False, f"budget: {used:,}/{budget:,} tokens used today"
    return True, ""



def _proxy_usage(inst, backend, raw, ms=None, turn="", step=None):
    """Book the tokens the UPSTREAM reports for this guest: the budget guard
    must not rest on what the agent chooses to tell us via /api/usage. The
    span (turn, step from the request headers, duration) rides along."""
    if inst is None:
        return
    raw = raw.strip()
    if raw.startswith(b"data:"):
        raw = raw[5:]
    try:
        j = json.loads(raw)
        u = j.get("usage") or {}
        if isinstance(u, dict) and (u.get("prompt_tokens") or u.get("completion_tokens")):
            _store.usage_add(inst["name"], j.get("model") or backend, u.get("prompt_tokens"),
                      u.get("completion_tokens"), u.get("cost"), turn=turn, ms=ms, step=step)
    except (ValueError, AttributeError, TypeError):
        pass
