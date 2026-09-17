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
import json
import shlex
import signal
import subprocess
import time

from mgr import instances as _instances
from mgr import notify as _notify
from mgr import paths as _paths
from mgr import settings as _settings
from mgr import util as _util
from mgr import guests as _guests
from mgr import host as _host
from mgr import mcp as _mcp
from mgr import mounts as _mounts
from mgr import netfw as _netfw


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
HARNESS_FILES = ("agent", "run_agent.py", "webterm.py")   # "agent" is the package directory
HARNESS_IMG = os.path.join(_paths.RUN_DIR, "harness.ext4")
HARNESS_ROOTFS = {"instances/openrouter-rootfs.ext4"}     # images built from AGENT_SRC
_harness_lock = threading.Lock()


def harness_sources():
    """All HARNESS_FILES under AGENT_SRC (a directory entry means every .py
    file below it) — or nothing: a half-present set (a partial rsync, a
    package mid-rename) must not become the drive a VM boots from;
    run_agent.py imports agent with no fallback."""
    if not AGENT_SRC:
        return []
    out = []
    for f in HARNESS_FILES:
        p = os.path.join(AGENT_SRC, f)
        if os.path.isdir(p):
            files = sorted(os.path.join(r, x) for r, _, fs in os.walk(p) for x in fs
                           if x.endswith(".py") and "__pycache__" not in r)
            if not files:
                return []
            out += files
        elif os.path.isfile(p):
            out.append(p)
        else:
            return []
    return out


def _harness_digest(srcs):
    h = hashlib.sha256()
    for p in srcs:
        h.update(os.path.relpath(p, AGENT_SRC).encode() + b"\0")
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
                dst = os.path.join(d, os.path.relpath(p, AGENT_SRC))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(p, dst)
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


# ---- firecracker lifecycle -------------------------------------------------
def guest_env(inst):
    """What the guest reads from config.env: the instance's config plus what
    the manager adds for this boot (paths, zone, DNS, exports)."""
    cfg = dict(inst.get("config", {}))
    # Second guard: older instance JSONs may still contain a key/MCP_CONFIG;
    # they still must not reach the disk.
    for k in _settings.NEVER_PERSIST:
        cfg.pop(k, None)
    # A minimal VM init does not have /usr/local/bin in PATH -> inject it so
    # claude/fabric are found (guest-init sources the config disk).
    cfg.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    cfg["FC_INSTANCE"] = inst["name"]   # for the host-folder reconciler in the guest
    cfg.setdefault("TZ", _host.HOST_TZ)        # the agent's clock: [Now] line per turn
    cfg["GUEST_DNS"] = _netfw.GUEST_DNS         # guest-init writes resolv.conf from it (site.json, not the image)
    cfg["AGENT_EXPORT"] = _mounts.workspace_dir(inst)   # its own workspace export, by absolute path
    if uses_harness(inst):
        cfg["MEMORY_DIR"] = "/memory"    # Markdown memory folder (mgr/memfs.py)
    if inst["name"] == _guests.ORCH_INSTANCE:   # only the orchestrator may manage tasks
        cfg["TASK_ADMIN"] = "1"
    # Key injection proxy active? Then the agent sends chat requests to the
    # manager instead of directly to the router — so the VM never sees an LLM key
    # (not even via the secret broker). The switch lives in the shared settings
    # so that ALL instances are switched over consistently.
    if _settings.load_settings().get("LLM_KEY_PROXY") == "1":
        cfg["KEY_PROXY"] = "1"
    return cfg


def make_config_disk(inst):
    """Create a small ext4 drive with the instance config (key=value) -> vdb."""
    cfg = guest_env(inst)
    d = os.path.join(_paths.RUN_DIR, f"{inst['name']}.cfgdir")
    os.makedirs(d, exist_ok=True)
    # Put tool plugins (firecracker/plugins/*.py) on the disk too — the agent
    # loads them at start from /config/plugins. New plugin = file + stop/start.
    pdst = os.path.join(d, "plugins")
    shutil.rmtree(pdst, ignore_errors=True)
    psrc = os.path.join(_paths.BASE, "plugins")
    if os.path.isdir(psrc):
        os.makedirs(pdst, exist_ok=True)
        for f0 in sorted(os.listdir(psrc)):
            sp = os.path.join(psrc, f0)
            if os.path.isdir(sp):                 # multi-file tool: whole folder
                shutil.copytree(sp, os.path.join(pdst, f0), dirs_exist_ok=True)
            elif f0.endswith(".py"):              # single .py (backwards compatible)
                shutil.copy2(sp, os.path.join(pdst, f0))
    with open(os.path.join(d, "config.env"), "w") as f:
        for k, v in cfg.items():
            # quote values (EXTRA_MOUNTS and others contain shell metacharacters like | and ;)
            f.write(f"{k}={shlex.quote(str(v))}\n")
    # The agent reads this disk as uid 1000: world-readable, whatever the
    # manager's umask (nothing on it is secret — NEVER_PERSIST above).
    for root, dirs, files in os.walk(d):
        os.chmod(root, 0o755)
        for f0 in files:
            os.chmod(os.path.join(root, f0), 0o644)
    img = os.path.join(_paths.RUN_DIR, f"{inst['name']}.config.ext4")
    mkfs_image(img, 16, "fcconfig", srcdir=d)
    return img



UPPER_SIZE_MB = 1024          # throwaway layer per start
UPPER_PERSIST_SIZE_MB = 4096  # persistent layer (apt/pip need room); sparse


def upper_path(inst):
    if inst.get("persist_disk"):
        return os.path.join(_paths.INST_DIR, f"{inst['name']}-upper.ext4")
    return os.path.join(_paths.RUN_DIR, f"{inst['name']}.upper.ext4")


def make_upper(inst):
    """Provide an empty (or, with persist, existing) upper image."""
    p = upper_path(inst)
    if inst.get("persist_disk") and os.path.exists(p):
        return p
    size = UPPER_PERSIST_SIZE_MB if inst.get("persist_disk") else UPPER_SIZE_MB
    if not mkfs_image(p, size, "fcupper"):
        raise RuntimeError(f"mkfs of the upper layer for {inst['name']} failed")
    return p


def reset_upper(name):
    """Delete the persistent write layer (factory reset). Only while stopped."""
    inst = next((i for i in _instances.load_instances() if i["name"] == name), None)
    if not inst:
        return "unknown"
    if _instances.is_running(inst):
        return "error: instance is running — stop it first"
    n = 0
    for p in (os.path.join(_paths.INST_DIR, f"{name}-upper.ext4"),
              os.path.join(_paths.RUN_DIR, f"{name}.upper.ext4")):
        try:
            os.remove(p); n += 1
        except OSError:
            pass
    return f"disk reset ({n} layer(s) removed)" if n else "nothing to reset"


def set_persist_disk(name, on):
    inst = next((i for i in _instances.load_instances() if i["name"] == name), None)
    if not inst:
        return "unknown"
    if inst.get("rootfs") not in OVERLAY_ROOTFS:
        return "error: this template's rootfs has no overlay support (yet)"
    inst["persist_disk"] = bool(on)
    _instances.save_instance(inst)
    running = " (applies after stop/start)" if _instances.is_running(inst) else ""
    return f"persistent disk for '{name}' {'ON' if on else 'off'}{running}"


def private_rootfs(inst):
    """Create a fresh rootfs copy for exactly this VM and return its path.

    All instances of a template pointed at the SAME ext4 image, writable. Two
    VMs running simultaneously then share one journal — that worked as long as
    barely anything was written, and ended on Aug 15 with 'error loading
    journal' at boot. Hence: a separate copy per start (sparse, ~seconds fast).
    Side effect, and a deliberate one: a restart always boots the current
    template image, rootfs updates take effect as before with stop/start. State
    that should persist doesn't live here anyway, but centrally (memory.json,
    chats.json, katfs)."""
    src = os.path.join(_paths.BASE, inst["rootfs"])
    dst = os.path.join(_paths.RUN_DIR, f"{inst['name']}.rootfs.ext4")
    tmp = dst + ".new"
    # --sparse=always: the 2-GB image carries ~550 MB; the copy should occupy
    # just as little. First .new, then rename — a half copy must never start as
    # a rootfs.
    _util.sh("cp", "--sparse=always", src, tmp)
    os.replace(tmp, dst)
    return dst


def _vdev(drives):
    """Guest device of the LAST drive in the list (virtio-blk: vda, vdb, …)."""
    return f"/dev/vd{chr(ord('a') + len(drives) - 1)}"


def gen_config(inst):
    n = _instances.net_of(inst)
    boot = (f"console=ttyS0 reboot=k panic=1 pci=off "
            f"ip={n['guest']}::{n['host']}:{n['mask']}::eth0:off init=/init")
    overlay = inst.get("rootfs") in OVERLAY_ROOTFS
    if overlay:
        drives = [{"drive_id": "rootfs", "path_on_host": os.path.join(_paths.BASE, inst["rootfs"]),
                   "is_root_device": True, "is_read_only": True}]
    else:
        drives = [{"drive_id": "rootfs", "path_on_host": private_rootfs(inst),
                   "is_root_device": True, "is_read_only": False}]
    cfg_disk = os.path.join(_paths.RUN_DIR, f"{inst['name']}.config.ext4")
    if os.path.exists(cfg_disk):
        drives.append({"drive_id": "config", "path_on_host": cfg_disk,
                       "is_root_device": False, "is_read_only": True})
    for j, d in enumerate(inst.get("extra_drives", [])):
        drives.append({"drive_id": f"data{j}", "path_on_host": d["path"],
                       "is_root_device": False, "is_read_only": d.get("readonly", False)})
    if uses_harness(inst):
        himg = harness_image()
        if himg:
            drives.append({"drive_id": "harness", "path_on_host": himg,
                           "is_root_device": False, "is_read_only": True})
            boot += f" fc_harness={_vdev(drives)}"
    if overlay:
        # Last drive = upper; the device name follows from the position
        # (virtio-blk: vda, vdb, ...). The guest reads it from /proc/cmdline.
        drives.append({"drive_id": "upper", "path_on_host": make_upper(inst),
                       "is_root_device": False, "is_read_only": False})
        boot += f" fc_upper={_vdev(drives)}"
    return {
        "boot-source": {"kernel_image_path": _paths.KERNEL, "boot_args": boot},
        "drives": drives,
        "network-interfaces": [{"iface_id": "eth0", "host_dev_name": n["tap"],
                                "guest_mac": n["mac"]}],
        "machine-config": {"vcpu_count": inst.get("vcpus", 2),
                           "mem_size_mib": inst.get("mem_mib", 1024)},
    }


def start(inst):
    if _instances.is_running(inst):
        return "already running"
    _netfw.ensure_net_base()
    _netfw.setup_tap(inst)
    _mounts.setup_mounts(inst)
    make_config_disk(inst)
    cfg = os.path.join(_paths.RUN_DIR, f"{inst['name']}.config.json")
    json.dump(gen_config(inst), open(cfg, "w"))
    sock = os.path.join(_paths.RUN_DIR, f"{inst['name']}.sock")
    log = open(os.path.join(_paths.RUN_DIR, f"{inst['name']}.log"), "ab")
    try:                                  # the operator may tail the console (root:operator, 0640)
        os.chmod(log.name, 0o640); os.chown(log.name, 0, _host.ADMIN_GID)
    except OSError:
        pass
    if os.path.exists(sock):
        os.remove(sock)
    p = subprocess.Popen([_paths.BIN, "--api-sock", sock, "--config-file", cfg],
                         stdout=log, stderr=log, start_new_session=True)
    open(_instances.pidfile(inst), "w").write(str(p.pid))
    return f"started (pid {p.pid})"


def stop(inst):
    pf = _instances.pidfile(inst)
    if os.path.exists(pf):
        try:
            os.kill(int(open(pf).read().strip()), signal.SIGTERM)
            time.sleep(1)
        except (ValueError, ProcessLookupError):
            pass
        os.remove(pf)
    _netfw.teardown_tap(inst)
    _mounts.teardown_mounts(inst)
    _mcp.mcp_hub_kill(inst["name"])
    # The private rootfs copy is worthless after stopping (the next start pulls
    # a fresh one) — just disk space, so remove it.
    for f in (f"{inst['name']}.rootfs.ext4", f"{inst['name']}.upper.ext4"):
        try:
            os.remove(os.path.join(_paths.RUN_DIR, f))
        except OSError:
            pass
    return "stopped"
