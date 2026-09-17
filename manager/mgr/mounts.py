# kAIm56 — self-hosted Firecracker AI-agent platform
# Copyright (C) 2026 the kAIm56 authors
# SPDX-License-Identifier: AGPL-3.0-or-later
# This program is free software under the GNU AGPL v3+; see LICENSE.
"""Host folders in a VM (security boundary): the guest squash user, the per-instance NFS exports of the workspace and the mounted host folders, and which host paths may never be mounted.

Part of the mgr package: no import from manager.py. Sibling modules are used
as ``_name.func`` (module attribute), so a test can replace one definition in
one place.
"""
import glob
import os
import pwd

from mgr import browse as _browse
from mgr import host as _host
from mgr import instances as _instances
from mgr import memfs as _memfs
from mgr import paths as _paths
from mgr import util as _util
from mgr import vm as _vm


# ---- NFS / host folders ----------------------------------------------------
# Every export is per instance and per guest IP: the workspace
# AGENT_ROOT/<instance> and the host folders bind-mounted under
# AGENT_ROOT/.fcmnt/<instance>/<idx>. There is NO pool-wide root export any
# more (it let every VM read and write every other VM's files, and crossmnt
# handed a client its neighbours' submounts); NFSv4 serves the exports from
# its pseudo-root, the guest mounts them by absolute path. Inside the VM the
# agent is uid 1000; on the host every access is squashed to GUEST_USER, a
# system user that owns nothing but these folders.
AGENT_ROOT = os.environ.get("AGENT_ROOT", os.path.join(_paths.HOME_DIR, "agent"))
AGENT_EXPORTS = "/etc/exports.d/agent.exports"       # the retired root export
EXPORTS_D = "/etc/exports.d"
FCMNT_ROOT = os.path.join(AGENT_ROOT, ".fcmnt")
GUEST_USER = os.environ.get("GUEST_USER", "kaim56-guest")
GUEST_UID = GUEST_GID = 1000          # until ensure_guest_user() resolved the user
def ensure_guest_user():
    """Resolve (root: create) the squash user. False when it does not exist
    and cannot be created — exports then fall back to uid 1000, as before."""
    global GUEST_UID, GUEST_GID
    try:
        pw_ = pwd.getpwnam(GUEST_USER)
    except KeyError:
        if os.geteuid() != 0:
            return False
        _util.sh("useradd", "-r", "-M", "-d", "/nonexistent", "-s", "/usr/sbin/nologin", GUEST_USER, check=False)
        try:
            pw_ = pwd.getpwnam(GUEST_USER)
        except KeyError:
            return False
    GUEST_UID, GUEST_GID = pw_.pw_uid, pw_.pw_gid
    return True


def own_guest_dir(path, mode=0o2750):
    """A folder the guests write: owned by the squash user, group = operator
    (setgid, so the operator can read what the agent produces), nobody else."""
    try:
        os.makedirs(path, exist_ok=True)
        if os.geteuid() == 0:
            os.chown(path, GUEST_UID, _host.ADMIN_GID)
        os.chmod(path, mode)
    except OSError as e:
        print(f"[quiet] own_guest_dir {path}: {e!r}", flush=True)


def workspace_dir(inst):
    return os.path.join(AGENT_ROOT, inst["name"])


def export_opts(ro, fsid):
    return (f"{'ro' if ro else 'rw'},sync,no_subtree_check,all_squash,"
            f"anonuid={GUEST_UID},anongid={GUEST_GID},fsid={fsid}")


# ---- host folders (NFS bind-mounts) ----------------------------------------
def retire_root_export():
    """Remove the old pool-wide workspace export (kept as .bak once). With it
    gone, a VM reaches exactly the folders exported to its own address."""
    if not os.path.exists(AGENT_EXPORTS):
        return False
    try:
        if not os.path.exists(AGENT_EXPORTS + ".bak"):
            os.replace(AGENT_EXPORTS, AGENT_EXPORTS + ".bak")
        else:
            os.remove(AGENT_EXPORTS)
        _util.sh("exportfs", "-ra", check=False)
        print(f"[nfs] retired the pool-wide export {AGENT_EXPORTS}", flush=True)
        return True
    except OSError as e:
        print(f"[quiet] retiring {AGENT_EXPORTS} failed: {e!r}", flush=True)
        return False


def mount_specs(inst):
    """Normalized host-folder mounts: bind target, NFS subpath, fsid, mode."""
    specs = []
    for j, m in enumerate(inst.get("mounts", []) or []):
        host = str(m.get("host", "")).strip()
        guest = str(m.get("guest", "")).strip()
        if not host or not guest:
            continue
        target = os.path.join(FCMNT_ROOT, inst["name"], str(j))
        specs.append({
            "idx": j, "host": host, "guest": guest,
            "ro": bool(m.get("readonly", False)),
            "target": target, "sub": target,        # NFSv4 pseudo-root: absolute path
            "fsid": 4000 + (inst.get("index", 0) % 200) * 16 + (j % 14),
        })
    # The instance's memory folder (mgr/memfs.py) rides the same mechanism:
    # exported to this guest only, mounted read-write at /memory. Slot 15 of
    # the fsid block is reserved for it, 14 for the workspace (user mounts 0..13).
    mem = _memfs.folder(inst["name"]) if _vm.uses_harness(inst) else None
    if mem:
        target = os.path.join(FCMNT_ROOT, inst["name"], "memory")
        specs.append({"idx": "memory", "host": mem, "guest": "/memory", "ro": False,
                      "target": target, "sub": target,
                      "fsid": 4000 + (inst.get("index", 0) % 200) * 16 + 15})
    return specs


def workspace_fsid(inst):
    return 4000 + (inst.get("index", 0) % 200) * 16 + 14


def desired_lines(inst):
    """The guest's mount list: one `sub|guest|ro|rw` line per host folder."""
    return "".join(f"{s['sub']}|{s['guest']}|{'ro' if s['ro'] else 'rw'}\n" for s in mount_specs(inst))


def write_desired(inst):
    """Write desired.list under .fcmnt/<inst>/ for the admin's eye. The guest
    does NOT read it any more: the workspace is shared by every VM, so any of
    them could have written another one's list — it asks /api/mounts (by
    source IP) instead."""
    d = os.path.join(FCMNT_ROOT, inst["name"])
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "desired.list"), "w") as f:
            for s in mount_specs(inst):
                f.write(f"{s['sub']}|{s['guest']}|{'ro' if s['ro'] else 'rw'}\n")
    except OSError as e:
        print(f"[quiet] desired.list for {inst.get('name')} failed: {e!r}", flush=True)


def setup_mounts(inst):
    """Export this instance's workspace and host folders to ITS address only."""
    ensure_guest_user()
    retire_root_export()
    n = _instances.net_of(inst)
    ws = workspace_dir(inst)
    own_guest_dir(ws)
    lines = [f"{ws} {n['guest']}({export_opts(False, workspace_fsid(inst))})\n"]
    for s in mount_specs(inst):
        if not os.path.isdir(s["host"]):
            continue  # missing host folder -> skip (do not create)
        os.makedirs(s["target"], exist_ok=True)
        _util.sh("umount", "-l", s["target"], check=False)   # release any old bind
        if _util.sh("mount", "--bind", s["host"], s["target"], check=False).returncode != 0:
            continue
        if s["ro"]:
            _util.sh("mount", "-o", "remount,ro,bind", s["target"], check=False)
        lines.append(f"{s['target']} {n['guest']}({export_opts(s['ro'], s['fsid'])})\n")
    os.makedirs(EXPORTS_D, exist_ok=True)
    with open(os.path.join(EXPORTS_D, f"fc-{inst['name']}.exports"), "w") as fh:
        fh.writelines(lines)
    _util.sh("exportfs", "-ra", check=False)
    write_desired(inst)   # the reconciler in the guest picks up the mounts


def guest_can_write(host):
    """Can the squash user write into this host folder? A read-write share of
    the operator's own folder is read-only for the agent unless the folder
    lets GUEST_USER in (chown / chmod g+w with the guest's group / o+w)."""
    try:
        st = os.stat(host)
    except OSError:
        return False
    if st.st_uid == GUEST_UID:
        return bool(st.st_mode & 0o200)
    if st.st_gid == GUEST_GID:
        return bool(st.st_mode & 0o020)
    return bool(st.st_mode & 0o002)


def teardown_mounts(inst):
    ef = os.path.join(EXPORTS_D, f"fc-{inst['name']}.exports")
    if os.path.exists(ef):
        os.remove(ef)
        _util.sh("exportfs", "-ra", check=False)
    d = os.path.join(FCMNT_ROOT, inst["name"])
    if os.path.isdir(d):
        # scan the actual contents (robust against leftovers): release
        # sub-binds, then remove empty directories (rmdir fails on busy/mount).
        for sub in os.listdir(d):
            p = os.path.join(d, sub)
            if os.path.isdir(p):
                _util.sh("umount", "-l", p, check=False)
        try:
            os.remove(os.path.join(d, "desired.list"))
        except OSError:
            pass
        for sub in os.listdir(d):
            try:
                os.rmdir(os.path.join(d, sub))
            except OSError:
                pass
        try:
            os.rmdir(d)
        except OSError:
            pass


_GUEST_MOUNT_DENY = ("/bin", "/sbin", "/usr", "/lib", "/lib32", "/lib64", "/etc", "/proc", "/sys",
                     "/dev", "/boot", "/run", "/var", "/app", "/harness", "/config", "/memory",
                     "/init", "/root", "/tmp")


def _protected_host_paths():
    out = [_paths.BASE, "/etc", "/root", "/var", "/usr", "/boot"]
    if _vm.AGENT_SRC:
        out.append(_vm.AGENT_SRC)                       # a VM writing agent.py = code in every VM
    for home in glob.glob("/home/*"):
        out += [os.path.join(home, d) for d in (".config", ".ssh", ".gnupg", ".claude")]
    return [os.path.realpath(p) for p in out]


def mount_error(host, guest):
    """'' when this host folder may be shared at this guest path, else why not.
    Host side: an existing directory under BROWSE_ROOTS that neither contains
    nor lies in the manager tree, the agent sources, ~/.config, ~/.ssh — in
    the VM the folder is uid 1000, on the host that is the owner of all of it.
    Guest side: an absolute path outside the system directories — a folder
    mounted over /bin runs the sharer's files as root at the next shell."""
    hp = os.path.realpath(str(host or ""))
    if not host or not os.path.isdir(hp):
        return f"host folder {host!r} is not a directory"
    if not any(hp == r or hp.startswith(r.rstrip("/") + "/") for r in _browse.BROWSE_ROOTS):
        return f"host folder must be under {', '.join(_browse.BROWSE_ROOTS)}"
    for prot in _protected_host_paths():
        if hp == prot or hp.startswith(prot + "/") or prot.startswith(hp + "/"):
            return f"host folder {host} would expose {prot}"
    g = str(guest or "")
    if not g.startswith("/") or g.rstrip("/") == "" or "/../" in g + "/" or "\n" in g:
        return f"guest path {g!r} must be an absolute path"
    gn = os.path.normpath(g)
    if any(gn == d or gn.startswith(d + "/") for d in _GUEST_MOUNT_DENY):
        return f"guest path {g} is a system directory"
    return ""


def set_mounts(name, mounts):
    inst = next((i for i in _instances.load_instances() if i["name"] == name), None)
    if not inst:
        return "unknown"
    old_specs = mount_specs(inst)
    wanted = [{"host": str(m.get("host", "")).strip(),
               "guest": str(m.get("guest", "")).strip(),
               "readonly": bool(m.get("readonly"))}
              for m in (mounts or [])
              if isinstance(m, dict) and m.get("host") and m.get("guest")]
    for m in wanted:
        why = mount_error(m["host"], m["guest"])
        if why:
            return f"error: {why}"
    inst["mounts"] = wanted
    ensure_guest_user()
    warn = [m["host"] for m in wanted if not m["readonly"] and not guest_can_write(m["host"])]
    _instances.save_instance(inst)
    note = ""
    if _instances.is_running(inst):
        # apply LIVE: tear down removed folders, export the current (new) ones.
        new_subs = {s["sub"] for s in mount_specs(inst)}
        for s in old_specs:
            if s["sub"] not in new_subs:
                _util.sh("umount", "-l", s["target"], check=False)
                try:
                    os.rmdir(s["target"])
                except OSError:
                    pass
        setup_mounts(inst)            # bind+export of the current folders (idempotent)
        write_desired(inst)           # the running guest mounts them itself (reconciler)
        note = " (applied live)"
    if warn:
        note += (f" — read-only for the agent until {GUEST_USER} may write there: "
                 + ", ".join(warn))
    return f"{len(inst['mounts'])} host folders saved{note}"
