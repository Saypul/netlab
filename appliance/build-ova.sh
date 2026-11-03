#!/usr/bin/env bash
# Build the NetLab virtual appliance (.ova) for VMware Workstation / Player / Fusion (Intel Macs).
#
#   appliance/build-ova.sh [ref]      ref: a release tag (default: the newest vX.Y.Z tag)
#
# Boots an Ubuntu 22.04 cloud image under QEMU/KVM, installs that NetLab release into /opt/netlab with
# install.sh, pre-pulls the router/PC container images, wipes per-machine state (users, keys, machine id)
# and packs the disk into dist/NetLab-<version>.ova. The appliance holds no GitHub token: updates ask for
# one in the web UI (version label in the header) or with `netlab token`.
# Needs: KVM, qemu-system-x86_64, qemu-img, python3-vmdkstream, git, curl, python3, ~10 GB free disk and internet.
set -euo pipefail
cd "$(dirname "$0")/.."

REF=${1:-$(git tag --list 'v*' --sort=-v:refname | head -1)}
[ -n "$REF" ] || { echo "no release tag; make one with ./release.sh first" >&2; exit 1; }
VERSION=$(git show "$REF:VERSION")
REPO=$(git remote get-url origin | sed -E 's#^(https://([^@/]*@)?github.com/|git@github.com:)##; s#\.git$##')
OUT=${NETLAB_DIST:-dist}
WORK=$OUT/build
NAME=NetLab-$VERSION
BASE_URL=https://cloud-images.ubuntu.com/releases/22.04/release/ubuntu-22.04-server-cloudimg-amd64.img
DISK_GB=80                       # virtual size; the .ova only carries the used part
BUILD_RAM=${BUILD_RAM_MB:-1536}  # RAM of the build VM on this machine
VM_CPUS=4 VM_RAM=8192            # what the appliance asks VMware for (changeable after import)

mkdir -p "$WORK/seed"
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "Ubuntu 22.04 cloud image"
[ -f "$OUT/ubuntu-22.04.img" ] || { curl -fsSL -C - -o "$OUT/ubuntu-22.04.img.part" "$BASE_URL" \
  && mv "$OUT/ubuntu-22.04.img.part" "$OUT/ubuntu-22.04.img"; }
rm -f "$WORK/disk.qcow2"
qemu-img create -q -f qcow2 -b "$(realpath "$OUT/ubuntu-22.04.img")" -F qcow2 "$WORK/disk.qcow2" "${DISK_GB}G"

say "NetLab $VERSION ($REF) for $REPO"
git bundle create -q "$WORK/seed/netlab.bundle" --branches --tags
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
SEED=http://10.0.2.2:$PORT  # the host, as the QEMU user-mode network shows it to the guest

cat > "$WORK/seed/meta-data" <<EOF
instance-id: netlab-build-$(date +%s)
local-hostname: netlab
EOF

# Everything that runs inside the build VM. Ends by writing NETLAB-BUILD-OK to the serial console.
cat > "$WORK/seed/build.sh" <<EOF
#!/bin/bash
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
trap 'echo NETLAB-BUILD-FAILED > /dev/ttyS0' ERR

# kernel with the VMware disk/network drivers (LSI Logic, E1000, VMXNET3) in the initramfs
echo MODULES=most > /etc/initramfs-tools/conf.d/netlab
apt-get update -q
apt-get install -y -q linux-generic open-vm-tools git
for m in mptspi e1000 vmxnet3; do modinfo -k "\$(ls /lib/modules | sort -V | tail -1)" \$m >/dev/null; done

curl -fsS $SEED/netlab.bundle -o /root/netlab.bundle
git clone -q /root/netlab.bundle /opt/netlab
git -C /opt/netlab checkout -q --detach $REF
git -C /opt/netlab remote set-url origin https://github.com/$REPO.git
rm /root/netlab.bundle
NETLAB_DIR=/opt/netlab bash /opt/netlab/install.sh
cd /opt/netlab
for img in \$(.venv/bin/python -c "import engine; print(*{t['image'] for t in engine.TEMPLATES.values() if t['kind'] == 'docker'})"); do
  docker pull -q "\$img"
done

# per-machine state is made again on the first boot of every imported copy
systemctl stop netlab
rm -rf data/users.json data/jwt_secret data/initial_password data/update*.json data/nat-dnsmasq.* data/ai
truncate -s 0 /etc/machine-id; rm -f /var/lib/dbus/machine-id
rm -f /etc/ssh/ssh_host_*

cat > /etc/systemd/system/netlab-firstboot.service <<'UNIT'
[Unit]
Description=NetLab appliance: new SSH host keys
ConditionPathExists=!/etc/ssh/ssh_host_ed25519_key
Before=ssh.service

[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A

[Install]
WantedBy=multi-user.target
UNIT

# console sign-in banner: the address to open and, until it is changed, the first admin password
cat > /usr/local/sbin/netlab-issue <<'SH'
#!/bin/sh
for _ in \$(seq 60); do [ -f /opt/netlab/data/users.json ] && break; sleep 1; done
dev=\$(ip route show default | awk '{print \$5; exit}')
{
  printf '%s\\n' "NetLab \$(cat /opt/netlab/VERSION)  -  \\\\n" ""
  printf '%s\\n' "  Open in a web browser:  http://\\\\4{\${dev:-eth0}}:8765/"
  if [ -f /opt/netlab/data/initial_password ]; then
    printf '%s\\n' "  First sign-in:          user admin, password \$(sed -n 's/^password: //p' /opt/netlab/data/initial_password)"
  fi
  printf '%s\\n' "" "  Console / SSH:          user root (first password: netlab)" \\
    "  Manage:                 netlab status | netlab update | netlab logs" ""
} > /etc/issue
agetty --reload 2>/dev/null || true
SH
chmod 755 /usr/local/sbin/netlab-issue
cat > /etc/systemd/system/netlab-issue.service <<'UNIT'
[Unit]
Description=NetLab appliance: console banner
After=netlab.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/netlab-issue

[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/netlab-issue.path <<'UNIT'
[Unit]
Description=NetLab appliance: refresh the console banner when the first password goes away

[Path]
PathChanged=/opt/netlab/data/initial_password

[Install]
WantedBy=multi-user.target
UNIT
systemctl enable -q netlab-firstboot.service netlab-issue.service netlab-issue.path

# any network card name (ens33 in VMware), DHCP
rm -f /etc/netplan/50-cloud-init.yaml
cat > /etc/netplan/01-netlab.yaml <<'YAML'
network:
  version: 2
  ethernets:
    lan:
      match: {name: "e*"}
      dhcp4: true
YAML
chmod 600 /etc/netplan/01-netlab.yaml

# root login on the console and SSH, password must be changed on first use
echo root:netlab | chpasswd
chage -d 0 root
printf 'PasswordAuthentication yes\nPermitRootLogin yes\n' > /etc/ssh/sshd_config.d/60-cloudimg-settings.conf
hostnamectl set-hostname netlab || echo netlab > /etc/hostname

cloud-init clean --logs --seed
touch /etc/cloud/cloud-init.disabled
apt-get clean
rm -rf /var/lib/apt/lists/* /root/.cache /tmp/*
journalctl --rotate; journalctl --vacuum-time=1s
fstrim -av
echo NETLAB-BUILD-OK > /dev/ttyS0
EOF

cat > "$WORK/seed/user-data" <<EOF
#cloud-config
ssh_pwauth: false
runcmd:
  - [bash, -c, "curl -fsS $SEED/build.sh -o /root/build.sh && bash /root/build.sh > /root/build.log 2>&1; tail -n 40 /root/build.log > /dev/ttyS0"]
power_state:
  mode: poweroff
  timeout: 30
EOF

say "Installing inside a build VM (takes 10-20 minutes)"
python3 -m http.server -b 127.0.0.1 -d "$WORK/seed" "$PORT" >/dev/null 2>&1 &
http=$!
trap 'kill $http 2>/dev/null || true' EXIT
timeout 3600 qemu-system-x86_64 -enable-kvm -cpu host -m "$BUILD_RAM" -smp 2 -display none -no-reboot \
  -drive file="$WORK/disk.qcow2",if=virtio,discard=unmap,detect-zeroes=unmap \
  -netdev user,id=n0 -device virtio-net-pci,netdev=n0 \
  -smbios "type=1,serial=ds=nocloud-net;s=$SEED/" \
  -serial file:"$WORK/console.log"
grep -q NETLAB-BUILD-OK "$WORK/console.log" || { tail -n 60 "$WORK/console.log"; echo "build failed" >&2; exit 1; }

say "Packing $OUT/$NAME.ova"
rm -rf "$WORK/ova" && mkdir "$WORK/ova"
# qemu-img's streamOptimized VMDK has no footer or end-of-stream marker, and VMware's OVA import then
# reports "SHA digest ... does not match manifest"; VMDKstream (Ubuntu's OVAs) follows VMware's layout.
python3 -c 'import VMDKstream' 2>/dev/null || apt-get install -y -q python3-vmdkstream
qemu-img convert -O raw "$WORK/disk.qcow2" "$WORK/disk.raw"  # sparse
rm -f "$WORK/disk.qcow2"
used=$(du -B1 "$WORK/disk.raw" | cut -f1)
python3 -c 'import sys, VMDKstream; VMDKstream.convert_to_stream(*sys.argv[1:])' "$WORK/disk.raw" "$WORK/ova/$NAME-disk1.vmdk"
qemu-img compare -q -f raw -F vmdk "$WORK/disk.raw" "$WORK/ova/$NAME-disk1.vmdk"
rm -f "$WORK/disk.raw"
vmdk_size=$(stat -c %s "$WORK/ova/$NAME-disk1.vmdk")
sed -e "s/@NAME@/$NAME/g; s/@VERSION@/$VERSION/g; s/@VMDK_SIZE@/$vmdk_size/; s/@USED@/$used/" \
    -e "s/@DISK_GB@/$DISK_GB/; s/@CPUS@/$VM_CPUS/g; s/@RAM@/$VM_RAM/g" appliance/netlab.ovf > "$WORK/ova/$NAME.ovf"
(cd "$WORK/ova" && for f in "$NAME.ovf" "$NAME-disk1.vmdk"; do echo "SHA256($f)= $(sha256sum "$f" | cut -d' ' -f1)"; done > "$NAME.mf")
tar -C "$WORK/ova" --format=ustar -cf "$OUT/$NAME.ova" "$NAME.ovf" "$NAME.mf" "$NAME-disk1.vmdk"
rm -rf "$WORK"
ls -lh "$OUT/$NAME.ova"
