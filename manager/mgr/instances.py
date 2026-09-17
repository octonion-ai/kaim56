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

from mgr import host as _host
from mgr import mcp as _mcp
from mgr import paths as _paths


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
