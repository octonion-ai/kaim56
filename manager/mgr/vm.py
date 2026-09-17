# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Firecracker VM lifecycle: config disk, overlay rootfs and harness drive, the per-instance write layer, start and stop, stale-image detection.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import hashlib
import os
import shutil
import tempfile
import threading

from mgr import instances as _instances
from mgr import notify as _notify
from mgr import paths as _paths
from mgr import settings as _settings
from mgr import util as _util


def mkfs_image(path, size_mb, label=None, srcdir=None):
    """Build an ext4 image atomically: a sparse file of size_mb, mkfs
    (populated from srcdir when given), renamed into place so a running VM
    keeps its old inode. False when mkfs fails — the old image, if any, stays."""
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".new",
                               dir=os.path.dirname(path) or ".")
    with os.fdopen(fd, "wb") as fh:
        fh.truncate(size_mb * 1024 * 1024)
    mkfs = shutil.which("mkfs.ext4", path="/usr/sbin:/sbin:" + os.environ.get("PATH", "")) or "mkfs.ext4"
    args = ["-F", "-q"] + (["-L", label] if label else []) + (["-d", srcdir] if srcdir else [])
    r = _util.sh(mkfs, *args, tmp, check=False)
    if r.returncode != 0:
        print(f"[mkfs] {os.path.basename(path)}: {r.stderr.strip()[:200]}", flush=True)
        os.unlink(tmp)
        return False
    os.replace(tmp, path)
    return True


# ---- Overlay rootfs ---------------------------------------------------------
# For images in OVERLAY_ROOTFS the VM boots with the SHARED base read-only
# (Firecracker blocks writes at the host level -> no journal conflict) plus a
# small rw upper image per instance; the guest init assembles the root from
# them via overlayfs+pivot_root. Advantage: no 2-GB copy per start, and with
# inst["persist_disk"]=true the write layer (installations!) survives a
# stop/start. Other images run unchanged via private_rootfs().
OVERLAY_ROOTFS = {"instances/openrouter-rootfs.ext4", "instances/claude-rootfs.ext4"}

# ---- Harness disk: the agent code as a read-only drive, not baked in ---------
# Pattern from Claude Code's sandbox (harness and skills are read-only shared
# layers next to the rootfs): the openrouter agent (agent.py, run_agent.py,
# webterm.py) lives on a small ext4 image the manager rebuilds from AGENT_SRC
# whenever the sources' CONTENT changes (a digest next to the image; mtimes
# lie after rsync, checkouts and clock skew), attached read-only to every VM
# on a rootfs that carries this agent. An agent change is then one instance
# restart — no docker build, no 2 GB image. The guest mounts it at /harness
# (boot arg fc_harness=/dev/vdX) and prefers it over /app; without the drive
# it boots from the rootfs as before.
AGENT_SRC = os.environ.get("AGENT_SRC") or _settings.SITE.get("AGENT_SRC") or ""
HARNESS_FILES = ("agent.py", "run_agent.py", "webterm.py")
HARNESS_IMG = os.path.join(_paths.RUN_DIR, "harness.ext4")
HARNESS_ROOTFS = {"instances/openrouter-rootfs.ext4"}     # images built from AGENT_SRC
_harness_lock = threading.Lock()


def harness_sources():
    """All HARNESS_FILES under AGENT_SRC — or nothing: a half-present set
    (agent.py mid-rename, a partial rsync) must not become the drive a VM
    boots from; run_agent.py imports agent with no fallback."""
    if not AGENT_SRC:
        return []
    ps = [os.path.join(AGENT_SRC, f) for f in HARNESS_FILES]
    return ps if all(os.path.isfile(p) for p in ps) else []


def _harness_digest(srcs):
    h = hashlib.sha256()
    for p in srcs:
        h.update(os.path.basename(p).encode() + b"\0")
        with open(p, "rb") as fh:
            h.update(fh.read())
        h.update(b"\0")
    return h.hexdigest()


def harness_image():
    """Path of the harness drive, (re)built when the sources' digest differs
    from the one recorded at the last build; None when AGENT_SRC is not
    configured or incomplete. Serialized: two starts (or the sweep and a
    start) must not build into the same file."""
    srcs = harness_sources()
    if not srcs:
        return None
    with _harness_lock:
        stamp = HARNESS_IMG + ".src"
        try:
            want = _harness_digest(srcs)
            with open(stamp) as fh:
                have = fh.read().strip()
        except OSError:
            have = ""
        if have == want and os.path.exists(HARNESS_IMG):
            return HARNESS_IMG
        d = tempfile.mkdtemp(prefix="harness-", dir=_paths.RUN_DIR)
        try:
            for p in srcs:
                shutil.copy2(p, os.path.join(d, os.path.basename(p)))
            if not mkfs_image(HARNESS_IMG, 8, "kaim56-harness", srcdir=d):
                return HARNESS_IMG if os.path.exists(HARNESS_IMG) else None
            with open(stamp, "w") as fh:
                fh.write(want)
            print(f"[harness] rebuilt from {AGENT_SRC} ({len(srcs)} files)", flush=True)
            return HARNESS_IMG
        finally:
            shutil.rmtree(d, ignore_errors=True)


def uses_harness(inst):
    """By image, not template name: llama/orcarouter share the openrouter
    rootfs and its agent, so they take (and go stale with) the same drive."""
    return inst.get("rootfs") in HARNESS_ROOTFS


def image_state(inst):
    """(stale, built, started): stale when a RUNNING VM on a shared base image
    was started before that image was last rebuilt — it still runs the old
    agent and will until stop/start. spawn_subagent was dead for three weeks
    and a tool fix missed the voice instance this way; nobody could see it."""
    if inst.get("rootfs") not in OVERLAY_ROOTFS or not _instances.is_running(inst):
        return False, 0, 0
    try:
        built = os.path.getmtime(os.path.join(_paths.BASE, inst["rootfs"]))
        if uses_harness(inst) and os.path.exists(HARNESS_IMG):
            built = max(built, os.path.getmtime(HARNESS_IMG))   # agent code counts too
        started = os.path.getmtime(_instances.pidfile(inst))
    except OSError:
        return False, 0, 0
    return started < built, built, started


def stale_instances():
    return [i["name"] for i in _instances.load_instances() if image_state(i)[0]]


_img_seen = {}      # rootfs path -> mtime last seen (filled at startup: no push for old news)


def image_sweep():
    """Idle worker: when a base image was rebuilt, push ONCE which running
    instances still sit on the old one. Stays quiet if nobody is affected."""
    hit = []
    try:
        harness_image()        # an edited agent.py shows up here, not at the next start
    except Exception as e:
        _util._wlog(f"image-sweep harness: {e!r}")
    for rel in sorted(OVERLAY_ROOTFS) + [HARNESS_IMG]:
        try:
            mt = os.path.getmtime(rel if os.path.isabs(rel) else os.path.join(_paths.BASE, rel))
        except OSError:
            continue
        if rel in _img_seen and mt > _img_seen[rel]:
            hit.append(rel)
        _img_seen[rel] = mt
    if not hit:
        return []
    old = stale_instances()
    if old:
        try:
            _notify.notify_add("rebuild", f"Rootfs rebuilt: {len(old)} instance(s) on the old image",
                       ", ".join(old) + " — restart them to pick up the new agent.",
                       link="instances")
        except Exception as e:
            _util._wlog(f"image-sweep notify: {e!r}")
    return old
