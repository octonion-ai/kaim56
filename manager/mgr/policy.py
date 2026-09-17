# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Policy (security boundary): the tool catalog and which tools an instance may use, the effective per-instance policy the UI shows, and what an ephemeral sandbox VM is allowed (tools, egress, no delegation).

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""

from mgr import instances as _instances
from mgr import mcp as _mcp
from mgr import paths as _paths
from mgr import personas as _personas
from mgr import policy as _policy
from mgr import secrets as _secrets
from mgr import skills as _skills

AGENT_TOOLS_CATALOG = [
    {"name": "bash", "desc": "Run shell commands in the workspace"},
    {"name": "read_file", "desc": "Read a file"},
    {"name": "write_file", "desc": "Write a file"},
    {"name": "write_xlsx", "desc": "Write a spreadsheet (.xlsx) into the workspace"},
    {"name": "write_docx", "desc": "Write a Word document (.docx) into the workspace"},
    {"name": "list_dir", "desc": "List a directory"},
    {"name": "offload_read", "desc": "Re-read offloaded (truncated) tool output"},
    {"name": "http_fetch", "desc": "Fetch a URL (HTTP)"},
    {"name": "read_pdf", "desc": "Extract PDF text (file or URL)"},
    {"name": "web_search", "desc": "Web search (DuckDuckGo) — needs internet"},
    {"name": "spawn_subagent", "desc": "Start an ephemeral subagent"},
    {"name": "create_task", "desc": "Queue a task (capable instance or ephemeral)"},
    {"name": "read_inbox", "desc": "Read new user messages (Signal/app/web)"},
    {"name": "list_tasks", "desc": "List running/scheduled tasks with IDs"},
    {"name": "delete_task", "desc": "Delete a running/scheduled task by ID"},
    {"name": "edit_task", "desc": "Change a task's message/schedule by ID"},
    {"name": "mission_start", "desc": "Create a mission: goal + steps (orchestrator only)"},
    {"name": "missions", "desc": "List open missions with status (orchestrator only)"},
    {"name": "mission_update", "desc": "Advance a mission step (orchestrator only)"},
    {"name": "mission_finish", "desc": "Complete a mission (orchestrator only)"},
    {"name": "send_signal", "desc": "Send a Signal message to the user (allowed numbers only)"},
    {"name": "notify", "desc": "Push notification to app + web manager (title + text)"},
    {"name": "ha_control", "desc": "Turn a Home Assistant device/area on or off by spoken name (matches + auto-learns aliases)"},
    {"name": "ha_learn_alias", "desc": "Teach Home Assistant a spoken-name alias for an entity (STT mishears names)"},
    {"name": "oracle", "desc": "Second opinion before risky actions (challenges assumptions, never acts)"},
    {"name": "list_agents", "desc": "Available agents + capabilities (routing)"},
    {"name": "recall_tasks", "desc": "Query earlier tasks/results (institutional knowledge)"},
    {"name": "list_skills", "desc": "List available skills"},
    {"name": "search_sessions", "desc": "Full-text search over earlier chats and task results"},
    {"name": "propose_skill", "desc": "Propose a reusable procedure as a skill (waits for approval)"},
    {"name": "load_skill", "desc": "Load a skill into the context"},
    {"name": "memory_store", "desc": "Remember a value permanently"},
    {"name": "memory_recall", "desc": "Retrieve a remembered value"},
    {"name": "memory_reflect", "desc": "Ask the second memory (Hindsight) a question over everything it has seen"},
    {"name": "playbook_add", "desc": "Record a permanent rule/playbook (always applies)"},
    {"name": "playbooks", "desc": "List playbooks (fixed rules)"},
    {"name": "playbook_forget", "desc": "Remove a playbook by ID"},
    {"name": "remote_ls", "desc": "List a katfs share"},
    {"name": "remote_read", "desc": "Read a katfs file"},
    {"name": "remote_write", "desc": "Write a katfs file"},
    {"name": "remote_delete", "desc": "Delete a katfs file/folder"},
    {"name": "list_secrets", "desc": "Show granted secret names"},
    {"name": "get_secret", "desc": "Fetch a granted secret"},
]
AGENT_TOOL_NAMES = {t["name"] for t in AGENT_TOOLS_CATALOG}


SANDBOX_DEFAULT_TOOLS = ["bash", "read_file", "write_file", "list_dir", "offload_read",
                         "http_fetch", "web_search", "read_pdf"]
SANDBOX_NEVER = {"spawn_subagent", "create_task", "send_signal", "notify", "get_secret", "list_secrets"}


def sandbox_config(caller, sandbox):
    """(cfg, internet, error) for an ephemeral VM from a sandbox request
    {"tools": …, "egress": …, "skill": name, "persona": name}.
    Empty request = the ephemeral VM as before (all tools, internet on)."""
    sb = sandbox or {}
    if not isinstance(sb, dict):
        return {}, True, "sandbox must be an object"
    ccfg = (caller or {}).get("config") or {}
    cat = ccfg.get("AGENT_TOOLS", "")
    caller_tools = {t.strip() for t in cat.split(",") if t.strip()} if cat else None   # None = all
    want = sb.get("tools") or []
    if isinstance(want, str):
        want = want.split(",")
    want = [str(t).strip() for t in want if str(t).strip()]
    skill = str(sb.get("skill") or "").strip()
    persona = str(sb.get("persona") or "").strip()
    pobj = next((p for p in _personas.load_personas() if p.get("name") == persona), None) if persona else None
    if persona and pobj is None:
        return {}, True, f"persona '{persona}' unknown"
    if not want and pobj and pobj.get("tools"):
        want = list(pobj["tools"])          # the persona's recommended tool subset
    if not want and skill:
        want = list(SANDBOX_DEFAULT_TOOLS)
    cfg = {}
    if want:
        unknown = sorted(set(want) - _policy.AGENT_TOOL_NAMES)
        if unknown:
            return {}, True, f"unknown tools: {', '.join(unknown)}"
        if caller_tools is not None:
            over = sorted(set(want) - caller_tools)
            if over:
                return {}, True, f"the caller does not hold these tools itself: {', '.join(over)}"
        cfg["AGENT_TOOLS"] = ",".join(sorted(set(want) - SANDBOX_NEVER))
    internet = True
    eg = sb.get("egress")
    if eg is not None and eg != "":
        if eg is False or (isinstance(eg, str) and eg.strip().lower() in ("none", "off", "no")):
            internet = False
        else:
            hosts = eg.split(",") if isinstance(eg, str) else list(eg)
            hosts = [str(x).strip().lower() for x in hosts if str(x).strip()]
            if not hosts:
                return {}, True, "egress: list hosts, or 'none'"
            ceg = [x.strip().lower() for x in (ccfg.get("EGRESS_ALLOW") or "").split(",") if x.strip()]
            if ceg:
                over = sorted(set(hosts) - set(ceg))
                if over:
                    return {}, True, f"egress outside the caller's own allowlist: {', '.join(over)}"
            cfg["EGRESS_ALLOW"] = ",".join(hosts)
    base = (pobj.get("prompt") if pobj else None) \
        or next((p.get("prompt", "") for p in _personas.load_personas() if p.get("name") == "assistant"), "") \
        or "You are a helpful agent with tools. Use them when needed, otherwise answer directly. Be concise."
    if pobj and pobj.get("model") and "OPENROUTER_MODEL" not in cfg:
        cfg["OPENROUTER_MODEL"] = str(pobj["model"])[:120]
    if skill:
        body = next((s.get("content", "") for s in _skills.load_skills() if s.get("name") == skill), None)
        if body is None:
            return {}, True, f"skill '{skill}' unknown"
        cfg["AGENT_SYSTEM"] = f"{base}\n\n[Skill: {skill}] Follow this skill for the task:\n{str(body)[:20000]}"
    elif pobj:
        cfg["AGENT_SYSTEM"] = base
    return cfg, internet, ""



def tool_allowed(inst, name):
    """A-2: enforce the per-instance AGENT_TOOLS allowlist at the HOST, not only
    in the guest. Empty allowlist = all tools (no behaviour change for the many
    instances that set none). A restricted instance is refused a capability it
    did not list — the allowlist becomes a real boundary, not a display hint."""
    at = ((inst or {}).get("config") or {}).get("AGENT_TOOLS", "")
    allow = {t.strip() for t in at.split(",") if t.strip()}
    return (not allow) or name in allow


def effective_policy(inst):
    """Everything an instance IS ALLOWED to do in one place: network, tools,
    secrets, MCP servers, model. Pulls the scattered controls (instance config,
    secret-policy) together into one view."""
    cfg = inst.get("config") or {}
    at = cfg.get("AGENT_TOOLS", "")
    tools_allowed = [t.strip() for t in at.split(",") if t.strip()] if at else None  # None = all
    model = cfg.get("OPENROUTER_MODEL") or cfg.get("PI_MODEL") or cfg.get("PRIME_MODEL") or ""
    mcps = [n for n in (cfg.get("MCP_SERVERS", "") or "").split(",") if n]
    return {
        "name": inst["name"],
        "template": inst.get("template", ""),
        "running": _instances.is_running(inst),
        "internet": inst.get("internet", True),
        "model": model,
        "tools_all": tools_allowed is None,
        "tools": tools_allowed if tools_allowed is not None else [t["name"] for t in _policy.AGENT_TOOLS_CATALOG],
        "secrets": sorted(_secrets.allowed_secret_keys(inst)),
        "mcps": mcps,
        "katfs_share": cfg.get("KATFS_SHARE", ""),
        "auto_reset": str(cfg.get("AUTO_RESET_MIN", "") or "0"),
    }


# mgr/mcp needs the secret functions; they are defined above by now.
_mcp.configure(_paths.BASE, _instances.load_instances, _secrets.allowed_secret_keys, _secrets.secret_store)
