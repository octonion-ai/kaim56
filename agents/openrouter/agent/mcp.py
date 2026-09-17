# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""MCP servers as tools: stdio servers started in the VM and the manager's MCP hub, discovered at init and exposed as tools.

Part of the openrouter agent package (runs inside the VM): no import from the package root. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import json
import os
import subprocess
import urllib.error

from . import config as _config
from . import mgrclient as _mgrclient


# --- MCP (stdio) ------------------------------------------------------------
class MCP:
    def __init__(self, name, argv, env=None):
        self.name = name
        proc_env = {**os.environ, **(env or {})}
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1, env=proc_env)
        self._id = 0
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "or-agent", "version": "1"}})
        self._notify("notifications/initialized")

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _rpc(self, method, params):
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        for _ in range(10000):
            line = self.proc.stdout.readline()
            if not line:
                return {}
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == self._id:
                return msg.get("result", {})
        return {}

    def tools(self):
        return self._rpc("tools/list", {}).get("tools", [])

    def call(self, tool, args):
        r = self._rpc("tools/call", {"name": tool, "arguments": args})
        parts = [c.get("text", "") for c in r.get("content", []) if c.get("type") == "text"]
        return "\n".join(parts) or json.dumps(r)[:_config.MAX_TOOL_OUT]


class HubMCP:
    """MCP via the manager instead of as its own process in the VM.

    The server process runs in the MCP hub on the host; here only JSON-RPC
    goes out via /api/mcp. This way the guest needs neither the tokens (the
    manager inserts them) nor LAN access (the hub opens the connection to the
    target system). Same interface as MCP: tools() and call()."""

    def __init__(self, name):
        self.name = name
        self._id = 0
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "or-agent", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def _send(self, payload):
        body = json.dumps({"server": self.name, "payload": payload})
        req = urllib.request.Request(_mgrclient._manager_base() + "/api/mcp", data=body.encode(),
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=120).read() or b"{}")

    def _rpc(self, method, params):
        self._id += 1
        out = self._send({"jsonrpc": "2.0", "id": self._id,
                          "method": method, "params": params})
        if out.get("error"):
            raise RuntimeError(str(out["error"])[:300])
        return out.get("result", {})

    def tools(self):
        return self._rpc("tools/list", {}).get("tools", [])

    def call(self, tool, args):
        r = self._rpc("tools/call", {"name": tool, "arguments": args})
        parts = [c.get("text", "") for c in r.get("content", []) if c.get("type") == "text"]
        return "\n".join(parts) or json.dumps(r)[:_config.MAX_TOOL_OUT]


_mcp = {}      # server-name -> MCP
_mcp_tools = {}  # exposed-tool-name -> (server-name, mcp-tool-name)


def init_mcp():
    # MCP_CONFIG no longer lives in the instance config — it carried the tokens in
    # plaintext. The manager assembles it at runtime from MCP_SERVERS and
    # inserts only the secrets that this instance's policy allows.
    cfg = os.environ.get("MCP_CONFIG", "")
    if not cfg:
        try:
            body = _mgrclient._mgr_get(_mgrclient._manager_base(), "/api/mcp-config")
            d = json.loads(body)
            if d.get("unresolved"):
                _config.log("MCP: secrets not released, server may start without access:",
                    ", ".join(d["unresolved"]))
            if d.get("mcpServers"):
                cfg = json.dumps(d)
        except Exception as e:
            _config.log("MCP configuration could not be obtained from the manager:", repr(e))
    if not cfg:
        p = os.path.join(_config.WORKDIR, ".mcp.json")
        if os.path.exists(p):
            cfg = open(p).read()
    if not cfg:
        return []
    try:
        servers = json.loads(cfg).get("mcpServers", json.loads(cfg))
    except Exception as e:
        _config.log("MCP config malformed:", e)
        return []
    schema = []
    for name, spec in servers.items():
        argv = [spec["command"], *spec.get("args", [])] if isinstance(spec, dict) else None
        if not argv:
            continue
        env = spec.get("env") if isinstance(spec, dict) else None
        try:
            # Hub first: the process runs on the host, the guest needs neither
            # argv nor env nor secrets. The own-process path stays a fallback
            # for managers without /api/mcp (older versions).
            try:
                srv = HubMCP(name)
            except Exception as hub_err:
                _config.log(f"MCP '{name}': hub unreachable ({hub_err!r:.120}), starting locally")
                srv = MCP(name, argv, env={str(k): str(v) for k, v in (env or {}).items()})
            _mcp[name] = srv
            for t in srv.tools():
                fq = f"{name}__{t['name']}"[:64]
                _mcp_tools[fq] = (name, t["name"])
                schema.append({"type": "function", "function": {
                    "name": fq, "description": (t.get("description") or fq)[:400],
                    "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}})
            _config.log(f"MCP '{name}': {len(srv.tools())} tools")
        except Exception as e:
            _config.log(f"MCP '{name}' start failed:", repr(e))
    return schema
