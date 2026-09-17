# kAIm56 on Proxmox VE

kAIm56 runs in a **virtual machine** on Proxmox, not in an LXC container: the
manager needs `/dev/kvm` for the Firecracker microVMs, creates tap interfaces
and iptables rules, and exports NFS shares from the kernel NFS server. A VM
with nested virtualization gives it all of that; a container does not.

Tested path: Proxmox VE 8, Debian 12 cloud image, `install.sh --with-voice`.

## 1. Nested virtualization on the Proxmox host

Check once on the Proxmox host (Intel; for AMD read `kvm_amd`):

```bash
cat /sys/module/kvm_intel/parameters/nested      # Y = fine
```

If it prints `N` or `0`:

```bash
echo "options kvm-intel nested=1" > /etc/modprobe.d/kvm-intel.conf    # AMD: kvm-amd
modprobe -r kvm_intel && modprobe kvm_intel                            # with no VM running
```

## 2. Create the VM

Sizing: every agent instance is a microVM with 1 GiB by default, the voice
container takes about 2 GB, the images about 10 GB of disk.

| | minimum | comfortable |
|---|---|---|
| vCPU | 4 | 8 |
| RAM | 8 GB | 16 GB |
| Disk | 40 GB | 100 GB |

The CPU type **must be `host`** — the default `x86-64-v2-AES` hides the
virtualization extensions and `/dev/kvm` never appears in the guest.

From the Proxmox shell, with a Debian 12 cloud image (adjust `local-lvm`,
the bridge and the VM id):

```bash
wget https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2

qm create 9056 --name kaim56 --memory 16384 --cores 8 --cpu host \
  --net0 virtio,bridge=vmbr0 --scsihw virtio-scsi-pci --ostype l26 --agent 1
qm importdisk 9056 debian-12-generic-amd64.qcow2 local-lvm
qm set 9056 --scsi0 local-lvm:vm-9056-disk-0,discard=on
qm resize 9056 scsi0 +90G
qm set 9056 --ide2 local-lvm:cloudinit --boot order=scsi0 --serial0 socket --vga serial0
qm set 9056 --ciuser kaim --sshkeys ~/.ssh/id_ed25519.pub --ipconfig0 ip=dhcp
qm start 9056
```

Via the web UI instead: Create VM → System: *Qemu Agent* on → Disks: 40 GB or
more, *Discard* on → CPU: type **host** → Memory: 8 GB or more → Network:
VirtIO on your bridge. Install Debian 12 from ISO, then continue below.

Cloud-init grows the root filesystem on first boot. Find the address with
`qm guest cmd 9056 network-get-interfaces` or on your DHCP server, then
`ssh kaim@<address>`.

## 3. Prepare the guest

```bash
ls -l /dev/kvm                                   # must exist — otherwise step 1/2 is incomplete
sudo apt-get update
sudo apt-get install -y docker.io git curl rsync e2fsprogs iptables python3
sudo usermod -aG docker "$USER" && newgrp docker  # or log out and in again
```

The installer adds the NFS server itself (`manager/setup-nfs-host.sh`).

## 4. Install kAIm56

```bash
git clone https://github.com/uneidel/kaim56 && cd kaim56
./install.sh --check
./install.sh --with-voice
```

About ten minutes the first time: Firecracker and the guest kernel are
downloaded, the agent rootfs and the host containers built, the systemd
service `firecracker-manager` installed and a smoke test run. The login for the
web UI is printed at the end and kept in `/etc/firecracker-manager.env`.

Then open `http://<address>:8700`, put an API key into *Settings* and create an
instance from a template. A second `./install.sh` run updates.

## 5. Network and firewall

- The microVMs sit on a private `/30` each and reach the internet through NAT
  on the VM's default interface (`ens18` on Proxmox). The installer detects it from
  the default route and writes it into the service unit; a line `HOSTIF=<nic>` in
  `/etc/firecracker-manager.env` overrides it and survives updates.
  Nothing needs to change on the bridge, no promiscuous mode.
- If the Proxmox VM firewall is on, allow **TCP 8700** in (the manager). The
  phone and the desktop client come in over iroh and need no port at all. For
  browser access from outside put a reverse proxy in front — a Traefik example
  is in `examples/traefik-agents.example.yml`.
- NFS (2049) is only reached from the microVMs inside the VM; do not expose it.

## 6. Backups and snapshots

Everything lives inside the VM: the runtime tree under the install user's home,
the secrets in `~/.config/kat56/secrets.env`, the state under `manager/`. A
Proxmox snapshot or a scheduled VM backup captures the whole installation;
stop the instances first (Instances tab) for a consistent copy of their
persistent disks.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `/dev/kvm missing` from `install.sh --check` | CPU type is not `host`, or nested virtualization is off on the Proxmox host |
| microVMs start but have no internet | the VM's default route is not on the interface the manager detected — `ip route` in the VM, then `HOSTIF` in `/etc/firecracker-manager.env` |
| instances mount no workspace | NFS server not running: `systemctl status nfs-server` in the VM, rerun `sudo manager/setup-nfs-host.sh` |
| everything is slow | ballooning or a shared host under memory pressure; give the VM fixed memory (`--balloon 0`) |
