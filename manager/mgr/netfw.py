# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Networking and firewall (security boundary): the tap per instance, anti-spoof rules, what a guest may reach on the host, and the per-instance egress rules (internet on/off, MCP and llama endpoints).

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import json
import os
import re
import socket

from mgr import host as _host
from mgr import instances as _instances
from mgr import mcp as _mcp
from mgr import settings as _settings
from mgr import util as _util


POOL = "172.30.0.0/16"


# ---- networking ------------------------------------------------------------
def ensure_net_base():
    _util.sh("sysctl", "-w", "net.ipv4.ip_forward=1", check=False)
    r = _util.sh("iptables", "-t", "nat", "-C", "POSTROUTING", "-s", POOL, "-o", _host.HOSTIF,
           "-j", "MASQUERADE", check=False)
    if r.returncode != 0:
        _util.sh("iptables", "-t", "nat", "-A", "POSTROUTING", "-s", POOL, "-o", _host.HOSTIF,
           "-j", "MASQUERADE", check=False)
    # Guest isolation: microVMs must NOT route to each other. A compromised
    # agent could otherwise reach another instance's chat/term ports (8080/7682,
    # bound to 0.0.0.0, no auth). Backstop DROP for pool->pool; the tap ACCEPTs
    # below are additionally scoped so they never even match guest-to-guest.
    # Guest->gateway (8700 broker) is host-local (INPUT) and unaffected by this.
    if _util.sh("iptables", "-C", "FORWARD", "-s", POOL, "-d", POOL, "-j", "DROP",
          check=False).returncode != 0:
        _util.sh("iptables", "-A", "FORWARD", "-s", POOL, "-d", POOL, "-j", "DROP", check=False)
    ensure_guest_input_rules()


# Guest -> host: what a VM legitimately needs from its gateway (.1 of the /30).
GUEST_INPUT_ACCEPT = (
    ("-p", "tcp", "--dport", str(_host.LISTEN[1])),          # manager: API, broker, LLM proxy
    ("-p", "tcp", "--dport", "2049"),                  # NFS workspace
    ("-p", "icmp"),                                    # ping the gateway
    ("-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED"),  # replies to host->guest (proxy)
)


def ensure_guest_input_rules():
    """Guest -> host is limited to the manager port and NFS. Without this every
    host service listening on 0.0.0.0 (sshd, rpcbind, …) is one hop away from
    each VM. The DROP goes in first so the ACCEPTs inserted afterwards sit
    above it; idempotent, so a restart adds nothing twice."""
    if _util.sh("iptables", "-C", "INPUT", "-i", "fc+", "-j", "DROP", check=False).returncode != 0:
        _util.sh("iptables", "-I", "INPUT", "1", "-i", "fc+", "-j", "DROP", check=False)
    for spec in GUEST_INPUT_ACCEPT:
        # Position matters, not just presence: an ACCEPT appended BELOW the
        # DROP (an older setup script did that for NFS) never matches, and a
        # presence check would leave it there. Remove every copy, insert on top.
        for _ in range(8):
            if _util.sh("iptables", "-D", "INPUT", "-i", "fc+", *spec, "-j", "ACCEPT", check=False).returncode != 0:
                break
        _util.sh("iptables", "-I", "INPUT", "1", "-i", "fc+", *spec, "-j", "ACCEPT", check=False)
    # A pool address arriving on the LAN interface is forged (a LAN box posing
    # as a stopped VM would pass every by-IP check): drop it first.
    if _host.HOSTIF and _util.sh("iptables", "-C", "INPUT", "-i", _host.HOSTIF, "-s", POOL, "-j", "DROP",
                     check=False).returncode != 0:
        _util.sh("iptables", "-I", "INPUT", "1", "-i", _host.HOSTIF, "-s", POOL, "-j", "DROP", check=False)


def _antispoof_rules(n):
    return [(chain, ("-i", n["tap"], "!", "-s", n["guest"], "-j", "DROP"))
            for chain in ("INPUT", "FORWARD")]


def ensure_antispoof(inst):
    """A VM's packets must carry its own /30 address: the source IP is the
    guest's identity for the manager (instance_by_ip), so a forged source would
    be a forged identity. Always on top — above the instance's FORWARD chain."""
    for chain, spec in _antispoof_rules(_instances.net_of(inst)):
        while _util.sh("iptables", "-C", chain, *spec, check=False).returncode == 0:
            _util.sh("iptables", "-D", chain, *spec, check=False)
        _util.sh("iptables", "-I", chain, "1", *spec, check=False)


def clear_antispoof(inst):
    for chain, spec in _antispoof_rules(_instances.net_of(inst)):
        while _util.sh("iptables", "-C", chain, *spec, check=False).returncode == 0:
            _util.sh("iptables", "-D", chain, *spec, check=False)


def setup_tap(inst):
    n = _instances.net_of(inst)
    _util.sh("ip", "link", "del", n["tap"], check=False)
    _util.sh("ip", "tuntap", "add", n["tap"], "mode", "tap")
    _util.sh("ip", "addr", "add", f"{n['host']}/30", "dev", n["tap"])
    _util.sh("ip", "link", "set", n["tap"], "up")
    # The host has FORWARD policy DROP + Docker chains in front of it -> generic
    # rules don't apply reliably. So allow tap traffic RIGHT AT THE TOP (before
    # DROP/Docker) — but ONLY to/from outside the pool. This lets the guest reach
    # the internet (destination not in the pool) and replies back (source not in
    # the pool), while guest-to-guest (both in the pool) matches no ACCEPT rule
    # and gets caught by the pool->pool DROP or the DROP policy.
    # Clear old, unrestricted ACCEPTs of the same tap first (the tap name is
    # reused on restart, otherwise the old hole would stay open).
    for spec in (["-i", n["tap"]], ["-o", n["tap"]]):
        while _util.sh("iptables", "-C", "FORWARD", *spec, "-j", "ACCEPT", check=False).returncode == 0:
            _util.sh("iptables", "-D", "FORWARD", *spec, "-j", "ACCEPT", check=False)
    apply_internet(inst, inst.get("internet", True))


# Until now, guests with internet=on could go anywhere — including the whole
# LAN. Home Assistant and Portainer were thus reachable from EVERY VM, whether
# the MCP was assigned to it or not (the broker protects the tokens, but the
# door stood open anyway). Now: internet yes, LAN no — except the endpoints of
# the MCPs listed in the instance's MCP_SERVERS, and the guests' DNS.
# DNS for the guests (ends up in resolv.conf via guest-init). Site-specific —
# set it via env on other installations; 1.1.1.1 works everywhere.
GUEST_DNS = os.environ.get("GUEST_DNS") or _settings.SITE.get("GUEST_DNS") or "1.1.1.1"
_PRIVATE_NETS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                 "100.64.0.0/10", "169.254.0.0/16")     # CGNAT/Tailscale, link-local too


def _mcp_endpoints(inst):
    """LAN targets (ip, port) that this instance needs according to MCP_SERVERS.
    Read from the catalog, not from the instance — the latter only holds names.
    IP literals only: a hostname in the catalog that resolves into the LAN would
    NOT be allowed here (deliberately; enter the IP instead)."""
    names = {x for x in (inst.get("config", {}).get("MCP_SERVERS", "") or "").split(",") if x}
    if not names:
        return []
    out = []
    for m in _mcp.load_mcps():
        if m.get("name") not in names:
            continue
        for scheme, host, port in re.findall(
                r"(https?)://(\d{1,3}(?:\.\d{1,3}){3})(?::(\d+))?", json.dumps(m)):
            try:
                import ipaddress
                if not ipaddress.ip_address(host).is_private:
                    continue          # public targets are covered by the internet rule
            except ValueError:
                continue
            out.append((host, int(port or (443 if scheme == "https" else 80))))
    return sorted(set(out))


def _llama_endpoint(inst):
    """(ip, port) of the llama.cpp server, if the instance uses it AND it is on
    the private network — then the gating must let it through. An endpoint on the
    host (reachable via the gateway) or on the internet needs no special rule."""
    ep = (inst.get("config", {}).get("LLAMA_ENDPOINT") or "").strip()
    if not ep:
        return None
    m = re.search(r"(https?)://(\d{1,3}(?:\.\d{1,3}){3})(?::(\d+))?", ep)
    if not m:
        return None
    import ipaddress
    scheme, host, port = m.group(1), m.group(2), m.group(3)
    try:
        if not ipaddress.ip_address(host).is_private:
            return None
    except ValueError:
        return None
    return (host, int(port or (443 if scheme == "https" else 80)))


def _fc_chain(inst):
    return "FC-" + re.sub(r"[^a-zA-Z0-9_.-]", "", inst["name"])[:24]


def apply_internet(inst, allow):
    """Set/remove the instance's egress rules. `allow=False` means: the VM may
    not leave its own /30 — no LAN, no internet. The manager broker at the
    gateway (8700) stays reachable (host-local, INPUT). And with it the LLM
    endpoint: an agent without internet CANNOT think.

    With allow=True the instance gets its own FORWARD chain:
      1. its MCP endpoints (tcp, targeted)     -> ACCEPT
      2. the guest DNS (53)                     -> ACCEPT
      3. private networks                       -> REJECT (not DROP: the
         agent should fail immediately, not run into a 30 s timeout)
      4. everything outside the pool (internet) -> ACCEPT
    The return path stays the generic rule: through NAT, replies are only
    possible for connections the guest opened itself."""
    n = _instances.net_of(inst)
    chain = _fc_chain(inst)

    # Clear out leftovers, idempotent: jump rule, chain, old direct rule.
    _util.sh("iptables", "-D", "FORWARD", "-i", n["tap"], "-j", chain, check=False)
    _util.sh("iptables", "-F", chain, check=False)
    _util.sh("iptables", "-X", chain, check=False)
    while _util.sh("iptables", "-C", "FORWARD", "-i", n["tap"], "!", "-d", POOL,
             "-j", "ACCEPT", check=False).returncode == 0:
        _util.sh("iptables", "-D", "FORWARD", "-i", n["tap"], "!", "-d", POOL,
           "-j", "ACCEPT", check=False)

    back = ["-o", n["tap"], "!", "-s", POOL]
    have_back = _util.sh("iptables", "-C", "FORWARD", *back, "-j", "ACCEPT", check=False).returncode == 0
    if not allow:
        if have_back:
            _util.sh("iptables", "-D", "FORWARD", *back, "-j", "ACCEPT", check=False)
        # Explicit, not by omission: "no network" used to rely on the FORWARD
        # policy being DROP — on a host where it is ACCEPT the switch did
        # nothing (found by a sandboxed sub-agent that curled the internet with
        # egress=none). The chain rejects everything outside the pool; the
        # manager at the gateway is INPUT, not FORWARD, and stays reachable.
        _util.sh("iptables", "-N", chain, check=False)
        _util.sh("iptables", "-A", chain, "!", "-d", POOL, "-j", "REJECT", check=False)
        _util.sh("iptables", "-I", "FORWARD", "1", "-i", n["tap"], "-j", chain, check=False)
        ensure_antispoof(inst)
        return

    _util.sh("iptables", "-N", chain, check=False)
    allow = list(_mcp_endpoints(inst))
    lp = _llama_endpoint(inst)
    if lp:
        allow.append(lp)
    for ip, port in allow:
        _util.sh("iptables", "-A", chain, "-d", ip, "-p", "tcp", "--dport", str(port),
           "-j", "ACCEPT", check=False)
    for proto in ("udp", "tcp"):
        _util.sh("iptables", "-A", chain, "-d", GUEST_DNS, "-p", proto, "--dport", "53",
           "-j", "ACCEPT", check=False)
    for net in _PRIVATE_NETS:
        _util.sh("iptables", "-A", chain, "-d", net, "-j", "REJECT", check=False)
    # Egress allowlist (guardrail): if EGRESS_ALLOW is in the instance config
    # (comma list of domains/IPs), the VM may go ONLY there — instead of
    # "everything except private". Domains are resolved at start (A records); a
    # stop/start is needed if the target's DNS changes. Empty = as before.
    egress = (inst.get("config", {}).get("EGRESS_ALLOW", "") or "").strip()
    if egress:
        seen = set()
        for host in [h.strip() for h in egress.split(",") if h.strip()]:
            try:
                infos = socket.getaddrinfo(host, None, socket.AF_INET)
                ips = sorted({i[4][0] for i in infos})
            except OSError:
                print(f"[egress] {inst['name']}: '{host}' not resolvable — skipped",
                      flush=True)
                continue
            for ip in ips:
                if ip not in seen:
                    seen.add(ip)
                    _util.sh("iptables", "-A", chain, "-d", ip, "-j", "ACCEPT", check=False)
        _util.sh("iptables", "-A", chain, "!", "-d", POOL, "-j", "REJECT", check=False)
    else:
        _util.sh("iptables", "-A", chain, "!", "-d", POOL, "-j", "ACCEPT", check=False)
    _util.sh("iptables", "-I", "FORWARD", "1", "-i", n["tap"], "-j", chain, check=False)
    if not have_back:
        _util.sh("iptables", "-I", "FORWARD", "1", *back, "-j", "ACCEPT", check=False)
    ensure_antispoof(inst)


def teardown_tap(inst):
    # Rules point at the tap NAME and survive deletion of the device — without
    # cleanup, dead chains pile up.
    n = _instances.net_of(inst)
    chain = _fc_chain(inst)
    _util.sh("iptables", "-D", "FORWARD", "-i", n["tap"], "-j", chain, check=False)
    _util.sh("iptables", "-F", chain, check=False)
    _util.sh("iptables", "-X", chain, check=False)
    clear_antispoof(inst)
    _util.sh("ip", "link", "del", n["tap"], check=False)
