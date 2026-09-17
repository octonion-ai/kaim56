# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Secrets broker (security boundary): the host secret store, what each instance may read (policy), and the claude credential file the claude template fetches at boot.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import json
import os
import re
import tempfile

from mgr import llmproxy as _llmproxy
from mgr import paths as _paths
from mgr import settings as _settings


# Subscription login of the claude template: the user's credential on the host.
# The manager runs as root and may read the 0600 file; the guest fetches it at
# boot via /api/claude-credentials (claude template only, by source IP).
# Defaults derive from the layout the installer lays out: the manager tree
# ($BASE/firecracker) sits next to the operator's home files, so the parent
# of BASE is the home; nothing here names a particular user.
CLAUDE_CRED_SRC = os.environ.get("CLAUDE_CRED_SRC", os.path.join(_paths.HOME_DIR, ".claude", ".credentials.json"))

# ---- Secrets broker (on-demand, allowlist per template/instance) -----------
SECRETS_FILE = os.environ.get("SECRETS_FILE", os.path.join(_paths.HOME_DIR, ".config", "kat56", "secrets.env"))
_SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def _secrets_write(lines):
    """Rewrite the store atomically: same directory, mode 0600, the owner
    the file had (the manager runs as root, the file is the operator's)."""
    d = os.path.dirname(SECRETS_FILE)
    os.makedirs(d, exist_ok=True)
    uid = gid = None
    try:
        st = os.stat(SECRETS_FILE); uid, gid = st.st_uid, st.st_gid
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=".secrets.", dir=d)
    with os.fdopen(fd, "w") as fh:
        fh.write("".join(lines))
    os.chmod(tmp, 0o600)
    if uid is not None and os.geteuid() == 0:
        os.chown(tmp, uid, gid)
    os.replace(tmp, SECRETS_FILE)


def secret_set(name, value):
    """Add or replace one value in the store. Names are SHOUTING_SNAKE, the
    value one line; other lines (order, comments) stay as they are. The
    value is never echoed back — the UI shows only that the key is set."""
    name = str(name or "").strip()
    if not _SECRET_NAME_RE.match(name):
        return "invalid name (A-Z, 0-9, _ ; 2-64 chars, starts with a letter)"
    value = str(value or "")
    if not value.strip() or "\n" in value or "\r" in value or len(value) > 4096:
        return "value must be one non-empty line (max 4096 chars)"
    try:
        lines = open(SECRETS_FILE).readlines() if os.path.exists(SECRETS_FILE) else []
    except OSError as e:
        return f"error: {e}"
    new, done = [], False
    for ln in lines:
        k = ln.split("=", 1)[0].strip() if "=" in ln and not ln.lstrip().startswith("#") else None
        if k == name:
            if not done:
                new.append(f"{name}={value}\n"); done = True
            continue                          # a duplicate line is dropped
        new.append(ln if ln.endswith("\n") else ln + "\n")
    if not done:
        new.append(f"{name}={value}\n")
    try:
        _secrets_write(new)
    except OSError as e:
        return f"error: {e}"
    print(f"[secrets] {'replaced' if done else 'added'} {name}", flush=True)
    return f"{name} {'replaced' if done else 'added'}"


def secret_delete(name):
    name = str(name or "").strip()
    if not _SECRET_NAME_RE.match(name):
        return "invalid name"
    try:
        lines = open(SECRETS_FILE).readlines() if os.path.exists(SECRETS_FILE) else []
    except OSError as e:
        return f"error: {e}"
    keep = [ln for ln in lines
            if not ("=" in ln and not ln.lstrip().startswith("#") and ln.split("=", 1)[0].strip() == name)]
    if len(keep) == len(lines):
        return f"{name} not in the store"
    try:
        _secrets_write(keep)
    except OSError as e:
        return f"error: {e}"
    print(f"[secrets] deleted {name}", flush=True)
    return f"{name} deleted"
SECRET_POLICY_FILE = os.path.join(_paths.BASE, "secret-policy.json")


def load_secrets_file():
    out = {}
    try:
        with open(SECRETS_FILE) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def secret_store():
    """All brokerable secrets. Source 1 is the secret store (0600). Source 2 is
    the manager settings — the LLM keys are maintained there, and since they no
    longer flow into the instance config, the broker has to deliver them. The
    store wins on a name collision."""
    out = dict(load_secrets_file())
    for k, v in _settings.load_settings().items():
        if k in _settings.SECRET_PARAMS and v and not out.get(k):
            out[k] = v
    return out


def load_secret_policy():
    try:
        with open(SECRET_POLICY_FILE) as fh:
            p = json.load(fh)
        if isinstance(p, dict):
            if "guest_readable" not in p:
                # Upgrade path: before the two-rights model every release was
                # readable raw. Seed the list from the releases ONCE so an
                # existing install keeps working; prune it in the Secrets tab.
                seed = sorted({k for grp in ("by_template", "by_instance")
                               for v in (p.get(grp) or {}).values() if isinstance(v, list)
                               for k in v if isinstance(k, str)})
                if (_settings.load_settings().get("LLM_KEY_PROXY") or "") == "1":
                    seed = [k for k in seed if k not in {kn for _, kn in _llmproxy.LLM_PROXY_UPSTREAMS.values()}]
                p["guest_readable"] = seed
                save_secret_policy(p)
                print(f"[secrets] guest_readable seeded from existing releases: {', '.join(seed) or '-'}", flush=True)
            return p
    except (FileNotFoundError, ValueError):
        pass
    return {"by_template": {}, "by_instance": {}, "guest_readable": []}


def guest_readable_keys(inst):
    """Keys a guest may fetch as RAW values through the broker. Two rights,
    two lists: a release in by_template/by_instance lets the HUB substitute
    the secret into an MCP config on the host; only a key that is ALSO in
    `guest_readable` ever leaves the host (get_secret). Since the hub and the
    LLM key proxy exist, that list is empty by default — a VM that needs a
    raw token is the exception, not the rule."""
    pol = load_secret_policy()
    return allowed_secret_keys(inst) & set(pol.get("guest_readable") or [])



def allowed_secret_keys(inst):
    """Effective allowlist = by_template[template] ∪ by_instance[name]. Default deny."""
    if not inst:
        return set()
    pol = load_secret_policy()
    keys = set(pol.get("by_template", {}).get(inst.get("template", ""), []))
    keys |= set(pol.get("by_instance", {}).get(inst.get("name", ""), []))
    return keys


def save_secret_policy(pol):
    """Save the policy (only {by_template,by_instance} with string lists)."""
    if not isinstance(pol, dict):
        return "invalid"
    clean = {"by_template": {}, "by_instance": {}, "guest_readable": []}
    for grp in ("by_template", "by_instance"):
        src = pol.get(grp, {})
        if isinstance(src, dict):
            for k, v in src.items():
                if isinstance(v, list):
                    clean[grp][str(k)] = [str(x) for x in v if isinstance(x, str)]
    gr = pol.get("guest_readable", [])
    if isinstance(gr, list):
        clean["guest_readable"] = sorted({str(x) for x in gr if isinstance(x, str)})
    try:
        with open(SECRET_POLICY_FILE, "w") as fh:
            json.dump(clean, fh, indent=2)
        return "saved"
    except OSError as e:
        return f"error: {e}"
