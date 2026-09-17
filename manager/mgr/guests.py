# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Guest identity and reach (security boundary): a VM is identified by its source IP; which POST and GET paths a guest may use at all; which other instances a guest may target.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
from mgr import instances as _instances


GUEST_POST_PATHS = ("/api/usage", "/api/audit", "/api/task", "/api/chat-log", "/api/trace",
                    "/api/skill-proposals", "/api/sessions-search",
                    "/api/stt", "/api/tts", "/api/signal", "/api/mcp",
                    "/api/memory-search", "/api/task-delete", "/api/task-edit",
                    "/api/playbook-add", "/api/playbook-remove", "/api/hitl",
                    "/api/notify", "/api/mission-start", "/api/mission-update",
                    "/api/mission-finish", "/api/ha-alias", "/api/ha-control")
GUEST_POST_PREFIXES = ("/api/memory/", "/api/llm/")

# GET paths a guest VM must never reach: the admin UI, the web chat, the katfs
# browser and the per-instance proxy /i/<name>/… (incl. the WebSocket
# terminal). Only POST was gated so far — a VM could open the SHELL of every
# other running VM through GET /i/<other>/term.
GUEST_GET_DENIED_EXACT = ("/", "/chat", "/katfs")
GUEST_GET_DENIED_PREFIXES = ("/i/", "/katfs/")


def guest_get_blocked(path):
    """True when a guest VM may not GET this path (query string ignored)."""
    p = path.split("?", 1)[0]
    if p != "/" and p.endswith("/") and p[:-1] in GUEST_GET_DENIED_EXACT:
        p = p[:-1]
    return p in GUEST_GET_DENIED_EXACT or p.startswith(GUEST_GET_DENIED_PREFIXES)

ORCH_INSTANCE = "orchestrator"


def delegate_targets(inst):
    """Instances this guest may address besides itself and 'ephemeral': the
    DELEGATE_TARGETS list of its config (comma-separated, '*' = all). The
    orchestrator may address everything — routing work is its job."""
    if inst.get("name") == ORCH_INSTANCE:
        return {"*"}
    raw = (inst.get("config") or {}).get("DELEGATE_TARGETS", "") or ""
    return {x.strip() for x in str(raw).split(",") if x.strip()}


def guest_may_target(inst, target):
    """May this guest create a task for (and see) `target`? Own name and
    'ephemeral' always, anything else only via DELEGATE_TARGETS. Closes the
    path where a prompt-injected agent runs its text on ANY other instance —
    with that instance's secrets and MCPs."""
    target = (target or "ephemeral").strip()
    if target in ("ephemeral", inst.get("name")):
        return True
    allow = delegate_targets(inst)
    return "*" in allow or target in allow

def instance_by_ip(ip):
    for i in _instances.load_instances():
        try:
            if _instances.net_of(i).get("guest") == ip:
                return i
        except Exception:
            continue
    return None
