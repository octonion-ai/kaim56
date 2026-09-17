# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Instances, the per-agent microVM records: instances/<name>.json, the templates, the network derived from an instance's index, where a VM's services listen, and whether it runs.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import json
import os
import re
import time

from mgr import host as _host
from mgr import mcp as _mcp
from mgr import paths as _paths
from mgr import hindsight as _hindsight
from mgr import memfs as _memfs
from mgr import mounts as _mounts
from mgr import netfw as _netfw
from mgr import policy as _policy
from mgr import secrets as _secrets
from mgr import settings as _settings
from mgr import skills as _skills
from mgr import store as _store
from mgr import vm as _vm


WEB_GUEST_PORT = 8080   # port of the web bridge in the microVM
TERM_GUEST_PORT = 7682  # port of the webterm (browser terminal) in the microVM


# ---- instances -------------------------------------------------------------
def save_instance(inst):
    """Instance JSON: written by root, readable by the operator's group — it
    carries no secrets by design (NEVER_PERSIST), and the tests, the build
    script's --smoke and the operator read it. Under umask 077 a plain
    open() would leave it root-only (load_instances then fails for anyone
    but root, seen on the deployment test VM)."""
    p = os.path.join(_paths.INST_DIR, f"{inst['name']}.json")
    with open(p, "w") as fh:
        json.dump(inst, fh, indent=2)
    try:
        os.chmod(p, 0o640)
        if os.geteuid() == 0:
            os.chown(p, 0, _host.ADMIN_GID)
    except OSError:
        pass


def load_instances():
    out = []
    for f in sorted(os.listdir(_paths.INST_DIR)) if os.path.isdir(_paths.INST_DIR) else []:
        if f.endswith(".json"):
            with open(os.path.join(_paths.INST_DIR, f)) as fh:
                out.append(json.load(fh))
    return out



def load_templates():
    out = []
    for f in sorted(os.listdir(_paths.TEMPLATE_DIR)) if os.path.isdir(_paths.TEMPLATE_DIR) else []:
        if f.endswith(".json"):
            with open(os.path.join(_paths.TEMPLATE_DIR, f)) as fh:
                out.append(json.load(fh))
    return out


def next_index():
    used = {i.get("index", 0) for i in load_instances()}
    n = 1
    while n in used:
        n += 1
    return n


# Tool catalog for the UI (mirrors BUILTIN in the openrouter agent). Display/
# allowlist only — the agent filters execution again itself.

def net_of(inst):
    i = inst["index"]
    return dict(host=f"172.30.{i}.1", guest=f"172.30.{i}.2", tap=f"fc{i}",
                mac=f"AA:FC:00:00:{i:02x}:01", mask="255.255.255.252")


def pidfile(inst):
    return os.path.join(_paths.RUN_DIR, f"{inst['name']}.pid")


def is_running(inst):
    pf = pidfile(inst)
    if not os.path.exists(pf):
        return False
    try:
        pid = int(open(pf).read().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False



def hindsight_retains(inst_name):
    """A-1: whether the second memory (Hindsight) keeps this instance's turns.
    Default on; HINDSIGHT_RETAIN=0 in the instance config turns it off entirely
    (a chatty voice agent should not fill its bank)."""
    inst = next((i for i in load_instances() if i.get("name") == inst_name), None)
    return ((inst or {}).get("config") or {}).get("HINDSIGHT_RETAIN", "1") != "0"



def mcp_servers_error(value):
    """'' when every name in a comma list is in the MCP catalog, else the
    complaint. Assigning a server that does not exist would only surface as
    'MCP start failed' in the guest log at the next start."""
    names = [x.strip() for x in str(value or "").split(",") if x.strip()]
    known = {m.get("name") for m in _mcp.load_mcps()}
    bad = [n for n in names if n not in known]
    return f"unknown MCP server(s): {', '.join(bad)}" if bad else ""


def create_instance(name, template, config=None, mounts=None, internet=True):
    name = "".join(c for c in name if c.isalnum() or c in "-_").lower()
    if not name:
        return "invalid name"
    if any(i["name"] == name for i in load_instances()):
        return f"'{name}' already exists"
    tpl = next((t for t in load_templates() if t.get("template") == template), None)
    if not tpl:
        return f"unknown template '{template}'"
    # defaults from template.params, overridden by the passed config,
    # empty values pre-filled from the shared settings
    cfg = {p["key"]: p.get("default", "") for p in tpl.get("params", [])}
    cfg.update({k: v for k, v in (config or {}).items() if v != ""})
    settings = _settings.load_settings()
    for k in list(cfg):
        if cfg[k] == "" and settings.get(k):
            cfg[k] = settings[k]
    for k in _settings.NEVER_PERSIST:
        cfg.pop(k, None)
    inst = {"name": name, "index": next_index(), "vcpus": tpl.get("vcpus", 2),
            "mem_mib": tpl.get("mem_mib", 1024), "rootfs": tpl["rootfs"],
            "internet": bool(internet),
            "description": f"{tpl.get('description','')} ({cfg.get('TRANSPORT','signal')}"
                           + (f", {cfg.get('FABRIC_MODEL')}" if cfg.get("FABRIC_MODEL") else "") + ")",
            "template": template, "config": cfg}
    clean = [{"host": str(m.get("host", "")).strip(),
              "guest": str(m.get("guest", "")).strip(),
              "readonly": bool(m.get("readonly"))}
             for m in (mounts or []) if isinstance(m, dict) and m.get("host") and m.get("guest")]
    if clean:
        inst["mounts"] = clean
    save_instance(inst)
    return f"instance '{name}' created from template '{template}'"


def set_instance_tools(name, tools):
    """Set an instance's tool allowlist. Empty/all list -> drop the field
    (= all tools). Takes effect at the next start (env-based)."""
    inst = next((i for i in load_instances() if i["name"] == name), None)
    if not inst:
        return "unknown"
    sel = [t for t in (tools or []) if t in _policy.AGENT_TOOL_NAMES]
    cfg = inst.setdefault("config", {})
    if sel and set(sel) != _policy.AGENT_TOOL_NAMES:
        cfg["AGENT_TOOLS"] = ",".join(sorted(sel))
    else:
        cfg.pop("AGENT_TOOLS", None)
    save_instance(inst)
    running = " (applies after stop/start)" if is_running(inst) else ""
    return f"tools for '{name}' saved{running}"


# Order = display logic in render()/list_agents: the first present key is the
# instance's model.
MODEL_KEYS = ("OPENROUTER_MODEL", "ORCAROUTER_MODEL", "ANTHROPIC_MODEL", "PI_MODEL", "PRIME_MODEL", "LLAMA_MODEL")
# For switching provider via set_model("provider:model"): provider name -> key.
PROVIDER_MODEL_KEY = {"openrouter": "OPENROUTER_MODEL", "orcarouter": "ORCAROUTER_MODEL",
                      "anthropic": "ANTHROPIC_MODEL", "pi": "PI_MODEL",
                      "prime": "PRIME_MODEL", "llama": "LLAMA_MODEL"}


def set_model(name, model):
    """Switch an existing instance's model. Sets exactly the key the instance
    already uses (no new one is invented — otherwise nobody would know which
    provider is meant). Takes effect at the next start (env-based), like the
    tool allowlist."""
    inst = next((i for i in load_instances() if i["name"] == name), None)
    if not inst:
        return "unknown"
    model = str(model or "").strip()
    if not model:
        return "error: no model given"
    cfg = inst.setdefault("config", {})
    # Provider switch: "orcarouter:tencent/hy3" additionally switches the backend
    # (sets its MODEL_KEY, removes the others). Without a prefix it stays with
    # the existing provider — only the model changes. The colon test triggers
    # ONLY for a known provider name, so ":free" model variants
    # (e.g. "mistralai/...:free") are not misread as a provider.
    if ":" in model and model.split(":", 1)[0] in PROVIDER_MODEL_KEY:
        prov, mdl = model.split(":", 1)
        key = PROVIDER_MODEL_KEY[prov]
        for k in MODEL_KEYS:
            cfg.pop(k, None)
        cfg[key] = mdl.strip()
        model = mdl.strip()
    else:
        key = next((k for k in MODEL_KEYS if k in cfg), None)
        if key is None:
            return (f"error: instance '{name}' has no model setting "
                    f"({'/'.join(MODEL_KEYS)})")
        cfg[key] = model
    save_instance(inst)
    running = " (applies after stop/start)" if is_running(inst) else ""
    return f"model for '{name}' set to {model}{running}"


def set_internet(name, on):
    inst = next((i for i in load_instances() if i["name"] == name), None)
    if not inst:
        return "unknown"
    inst["internet"] = bool(on)
    save_instance(inst)
    if is_running(inst):
        _netfw.apply_internet(inst, on)   # takes effect immediately, no restart needed
    return f"internet for '{name}': {'on' if on else 'off'}"


def delete_instance(name):
    inst = next((i for i in load_instances() if i["name"] == name), None)
    if not inst:
        return "unknown"
    if is_running(inst):
        _vm.stop(inst)
    _mounts.teardown_mounts(inst)   # safely remove any leftovers (binds/export)
    p = os.path.join(_paths.INST_DIR, f"{name}.json")
    if os.path.exists(p):
        os.remove(p)
    return f"instance '{name}' deleted"



TEMPLATE_RUNTIME = {"openrouter": "openrouter-agent", "orcarouter": "openrouter-agent",
                    "llama": "openrouter-agent (local model)", "claude": "claude-code",
                    "pi": "pi", "prime": "prime"}


def claude_login_state():
    """The host's Claude login as the session panel shows it: not only present,
    but how long its access token is still valid (the VM works from a copy)."""
    try:
        with open(_secrets.CLAUDE_CRED_SRC) as fh:
            exp = (json.load(fh).get("claudeAiOauth") or {}).get("expiresAt") or 0
    except (OSError, ValueError):
        return "missing (log in on the host)"
    left = exp / 1000 - time.time()
    if left <= 0:
        return "expired on the host (run claude /login there)"
    return f"ok · valid {int(left // 3600)}h {int(left % 3600 // 60)}m"


def session_info(inst):
    """What the chat's session panel shows for an instance: runtime, uptime,
    login state, the platform services as the agent sees them, and its MCP
    servers with whether their secrets are released. Nothing secret in it."""
    name, tpl = inst["name"], inst.get("template", "")
    cfg = inst.get("config") or {}
    running = is_running(inst)
    try:
        started = os.path.getmtime(pidfile(inst)) if running else 0
    except OSError:
        started = 0
    if tpl == "claude":
        login = claude_login_state()
    elif (_settings.load_settings().get("LLM_KEY_PROXY") or "") == "1":
        login = "key proxy"
    else:
        keyname = "ORCAROUTER_API_KEY" if tpl in ("orcarouter", "llama") else "OPENROUTER_API_KEY"
        login = "api key" if _secrets.secret_store().get(keyname) else "no key"
    mem_dir = _memfs.folder(name) if _vm.uses_harness(inst) else None
    notes = 0
    if mem_dir:
        try:
            notes = len([f for f in os.listdir(os.path.join(mem_dir, "notes")) if f.endswith(".md")])
        except OSError:
            pass
    try:
        with _store._hist_lock, _store._hist_conn() as c:
            sem = c.execute("SELECT COUNT(*) FROM semantic_memory WHERE instance=?", (name,)).fetchone()[0]
    except Exception:
        sem = 0
    platform = [
        {"name": "Memory", "state": (f"{notes} notes · {sem} semantic" if (notes or sem) else "empty")
                            + (" · hindsight" if _hindsight.enabled() else ""), "ok": True},
        {"name": "Web search", "state": "reachable" if (_settings.load_settings().get("BRAVE_API_KEY") or "") else "DuckDuckGo fallback", "ok": True},
        {"name": "Skills", "state": f"{len(_skills.load_skills())} in catalog", "ok": True},
        {"name": "Traces", "state": f"{len(_store.turns_read(name, limit=50))} recent turns", "ok": True},
    ]
    allowed = _secrets.allowed_secret_keys(inst)
    mcps = []
    for n in [x for x in (cfg.get("MCP_SERVERS", "") or "").split(",") if x]:
        missing = sorted(_mcp.mcp_required_secrets([n]) - allowed)
        mcps.append({"name": n, "ready": not missing, "missing": missing})
    return {"name": name, "template": tpl, "runtime": TEMPLATE_RUNTIME.get(tpl, tpl or "agent"),
            "running": running, "uptime": int(time.time() - started) if started else 0,
            "model": cfg.get("OPENROUTER_MODEL") or cfg.get("ANTHROPIC_MODEL") or "",
            "stale": _vm.image_state(inst)[0], "login": login,
            "platform": platform, "mcps": mcps,
            "need_secret": sum(1 for m in mcps if not m["ready"])}



def _set_config_key(name, key, val):
    """Set/delete a single config key (secrets stay out — broker only)."""
    inst = next((i for i in load_instances() if i["name"] == name), None)
    if not inst:
        return "unknown"
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,40}", key) or key in _settings.NEVER_PERSIST:
        return f"error: key '{key}' not allowed"
    if key == "MCP_SERVERS" and mcp_servers_error(val):
        return "error: " + mcp_servers_error(val)
    cfg = inst.setdefault("config", {})
    if val in ("", None):
        cfg.pop(key, None)
    else:
        cfg[key] = str(val)
    save_instance(inst)
    return (f"{key} " + ("removed" if val in ("", None) else f"= {val}")
            + (" (applies after stop/start)" if is_running(inst) else ""))
