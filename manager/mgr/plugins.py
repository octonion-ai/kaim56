# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Tool plugins (drag and drop in the web manager): storage, content-hash pinning, listing, write and delete.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import hashlib
import io
import json
import os
import re
import shutil
import urllib.request
import zipfile

from mgr import paths as _paths
from mgr import settings as _settings


# ---- Manage tool plugins (drag & drop in the web manager) ------------------
# Each tool = a folder plugins/<name>/ with an entry file tool.py (convention
# DESC/PARAMS/REQUIRED/run). Single .py files are stored as plugins/<name>/tool.py.
# The folder is copied onto the config disk when the instance starts and loaded
# inside the VM (sandbox). stdlib-only.
PLUGINS_SRC = os.path.join(_paths.BASE, "plugins")
PLUGIN_MAX_BYTES = 5 * 1024 * 1024
PLUGIN_BOILERPLATE = (
    "# Tool plugin for kAIm56. Convention: DESC / PARAMS / REQUIRED / run().\n"
    "# Runs in the agent VM (sandbox), stdlib-only. Multiple files? Put more\n"
    "# .py files in this folder and import them here (e.g. `import helper`).\n"
    "DESC = \"Short: what the tool does (shown to the model as the tool description).\"\n"
    "PARAMS = {\n"
    "    \"text\": {\"type\": \"string\", \"description\": \"example parameter\"},\n"
    "}\n"
    "REQUIRED = []\n"
    "\n"
    "def run(text=\"\"):\n"
    "    # ... your logic; return a string ...\n"
    "    return f\"ok: {text}\"\n"
)


def _safe_tool_name(name):
    return re.sub(r"[^a-z0-9_-]", "", (name or "").strip().lower())[:40]


# ---- Plugin integrity: content-hash pinning (idea from MS "APM") -----------
# On upload/creation the SHA-256 over all of the tool's files is recorded as
# "approved". If a plugin file is later changed directly (bypassing the UI),
# the hash diverges -> the UI shows "modified" and you must deliberately re-pin
# the change via "Approve". Runtime state, gitignored.
PLUGIN_PINS_FILE = os.path.join(PLUGINS_SRC, ".pins.json")


def _plugin_hash(name):
    """SHA-256 over (relpath\0content\0) of all a tool's files, sorted."""
    name = _safe_tool_name(name)
    folder = os.path.join(PLUGINS_SRC, name)
    single = os.path.join(PLUGINS_SRC, name + ".py")
    if os.path.isdir(folder):
        files, base = [], folder
        for root, _d, fs in os.walk(folder):
            for f in fs:
                files.append(os.path.join(root, f))
        files.sort()
    elif os.path.isfile(single):
        files, base = [single], PLUGINS_SRC
    else:
        return None
    h = hashlib.sha256()
    for fp in files:
        h.update(os.path.relpath(fp, base).encode()); h.update(b"\0")
        try:
            with open(fp, "rb") as fh:
                h.update(fh.read())
        except OSError:
            return None
        h.update(b"\0")
    return h.hexdigest()


def load_plugin_pins():
    try:
        with open(PLUGIN_PINS_FILE) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _save_plugin_pins(d):
    try:
        with open(PLUGIN_PINS_FILE, "w") as fh:
            json.dump(d, fh, indent=2)
    except OSError as e:
        # unpinned plugins = the integrity check silently stops checking
        print(f"[quiet] plugin pins save failed: {e!r}", flush=True)


def plugin_pin(name):
    """Record the current state as approved (upload or 'Approve')."""
    name = _safe_tool_name(name)
    h = _plugin_hash(name)
    pins = load_plugin_pins()
    if h:
        pins[name] = h
    else:
        pins.pop(name, None)
    _save_plugin_pins(pins)
    return h


def plugin_code_link(kind, name, rel):
    """URL that opens one plugin file in the host's VS Code (code-server):
    `?folder=<CODE_ROOT>&payload=[["openFile","vscode-remote://<authority>/<path>"]]`
    — the workbench reads `openFile` from the payload, and code-server's remote
    authority is its own host:port. Needs CODE_URL and CODE_ROOT; else ''."""
    if not (_settings.CODE_URL and _settings.CODE_ROOT):
        return ""
    u = urllib.parse.urlsplit(_settings.CODE_URL)
    base = _settings.CODE_ROOT.rstrip("/") + "/plugins/"
    path = base + (name + "/" + rel if kind == "folder" else rel)
    payload = json.dumps([["openFile", f"vscode-remote://{u.netloc}{path}"]],
                         separators=(",", ":"))
    q = urllib.parse.urlencode({"folder": _settings.CODE_ROOT.rstrip("/"), "payload": payload})
    return urllib.parse.urlunsplit((u.scheme, u.netloc, "/", q, ""))


def list_plugins():
    out = []
    if not os.path.isdir(PLUGINS_SRC):
        return out
    for entry in sorted(os.listdir(PLUGINS_SRC)):
        if entry.startswith((".", "__")):        # __pycache__, hidden
            continue
        path = os.path.join(PLUGINS_SRC, entry)
        if os.path.isdir(path):
            files = []
            for root, _dirs, fs in os.walk(path):
                for f in fs:
                    rel = os.path.relpath(os.path.join(root, f), path)
                    files.append(rel)
            out.append({"name": entry, "kind": "folder", "files": sorted(files)})
        elif entry.endswith(".py"):
            out.append({"name": entry[:-3], "kind": "file", "files": [entry]})
    pins = load_plugin_pins()
    for e in out:
        cur = _plugin_hash(e["name"]); pin = pins.get(e["name"])
        e["sha"] = (cur or "")[:12]
        e["pinned"] = bool(pin)
        e["modified"] = bool(pin) and cur is not None and cur != pin
        e["links"] = {f: plugin_code_link(e["kind"], e["name"], f) for f in e["files"]}
    return out


def plugin_write_py(name, code):
    name = _safe_tool_name(name)
    if not name:
        return "invalid name"
    dest = os.path.join(PLUGINS_SRC, name)
    os.makedirs(dest, exist_ok=True)
    with open(os.path.join(dest, "tool.py"), "w", encoding="utf-8") as fh:
        fh.write(code or PLUGIN_BOILERPLATE)
    plugin_pin(name)
    return None


def plugin_write_zip(name, raw):
    name = _safe_tool_name(name)
    if not name:
        return "invalid name"
    dest = os.path.join(PLUGINS_SRC, name)
    dest_abs = os.path.abspath(dest)
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return "broken zip"
    names = [n for n in z.namelist() if not n.endswith("/")]
    tops = {n.split("/", 1)[0] for n in names}
    strip = len(tops) == 1 and any("/" in n for n in names)
    top = next(iter(tops)) if strip else None
    for m in z.infolist():
        if m.is_dir():
            continue
        rel = m.filename[len(top) + 1:] if strip else m.filename
        if not rel or rel.startswith("__MACOSX"):
            continue
        target = os.path.normpath(os.path.join(dest, rel))
        if not (target == dest_abs or target.startswith(dest_abs + os.sep)):
            continue   # zip-slip protection
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with z.open(m) as fsrc, open(target, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst)
    if not any(os.path.isfile(os.path.join(dest, c))
               for c in ("tool.py", "__init__.py", name + ".py")):
        return "no entry (tool.py/__init__.py) found in the zip"
    plugin_pin(name)
    return None


def plugin_delete(name):
    name = _safe_tool_name(name)
    pins = load_plugin_pins()
    if pins.pop(name, None) is not None:
        _save_plugin_pins(pins)
    dfolder = os.path.join(PLUGINS_SRC, name)
    dfile = os.path.join(PLUGINS_SRC, name + ".py")
    if os.path.isdir(dfolder):
        shutil.rmtree(dfolder, ignore_errors=True)
        return True
    if os.path.isfile(dfile):
        os.remove(dfile)
        return True
    return False
