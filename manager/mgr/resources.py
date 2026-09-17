# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""The Resources tab: CPU, memory and disk per running instance, read from the firecracker process and the write layer.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import os
import time

from mgr import instances as _instances
from mgr import vm as _vm


# ---- Resource overview per instance (Resources tab) ------------------------
def _read_pid(inst):
    try:
        return int(open(_instances.pidfile(inst)).read().strip())
    except (OSError, ValueError):
        return None


def _proc_cpu_jiffies(pid):
    """utime+stime from /proc/<pid>/stat, robust against spaces in comm."""
    try:
        with open("/proc/%d/stat" % pid) as fh:
            after = fh.read().rpartition(")")[2].split()
        return int(after[11]) + int(after[12])   # utime (field 14) + stime (field 15)
    except (OSError, ValueError, IndexError):
        return None


def _proc_rss_kb(pid):
    try:
        with open("/proc/%d/status" % pid) as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


def resource_stats():
    """Per instance: configured size (vCPU/RAM) + live usage (RSS, CPU%,
    overlay disk). CPU% via a short sample; percentages relative to ONE core
    (a 2-vCPU guest can reach up to ~200%)."""
    insts = _instances.load_instances()
    clk = os.sysconf("SC_CLK_TCK") or 100
    pids = {i["name"]: _read_pid(i) for i in insts}
    pids = {n: p for n, p in pids.items() if p is not None and os.path.exists("/proc/%d" % p)}
    t0 = {n: _proc_cpu_jiffies(p) for n, p in pids.items()}
    dt = 0.3
    time.sleep(dt)
    t1 = {n: _proc_cpu_jiffies(p) for n, p in pids.items()}
    out = []
    for i in insts:
        name = i["name"]
        running = name in pids
        rss = _proc_rss_kb(pids[name]) if running else None
        j0, j1 = t0.get(name), t1.get(name)
        cpu_pct = round(100.0 * (j1 - j0) / (clk * dt), 1) if (j0 is not None and j1 is not None) else None
        try:
            st = os.stat(_vm.upper_path(i)); upper_used_mb = round(st.st_blocks * 512 / 1048576.0, 1)
        except OSError:
            upper_used_mb = None
        out.append({
            "name": name, "running": running,
            "vcpus": i.get("vcpus", 2), "mem_mib": i.get("mem_mib", 1024),
            "rss_mb": round(rss / 1024.0, 1) if rss else None,
            "cpu_pct": cpu_pct,
            "persist": bool(i.get("persist_disk")),
            "upper_used_mb": upper_used_mb,
        })
    return out
