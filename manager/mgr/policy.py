# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Policy (security boundary): the tool catalog and which tools an instance may use, the effective per-instance policy the UI shows, and what an ephemeral sandbox VM is allowed (tools, egress, no delegation).

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""

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
