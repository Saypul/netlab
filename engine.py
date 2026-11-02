"""NetLab engine: lab storage, node drivers (Docker / QEMU) and lab wiring.

Wiring model (similar to EVE-NG): every node interface is a host-side port
(veth for containers, tap for QEMU). Every link or network object is a Linux
bridge. Connecting = enslaving the host-side port to the bridge, so links can
be added or removed while nodes are running.

All host objects are prefixed so they never collide with existing Docker
networks: containers `nl-*`, bridges `nlb*`, ports `nlv*`.
"""
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("NETLAB_DATA", BASE / "data"))
LABS = DATA / "labs"
NODES = DATA / "nodes"
IMAGES = DATA / "images" / "qemu"
for d in (LABS, NODES, IMAGES):
    d.mkdir(parents=True, exist_ok=True)

FRR_IMAGE = "quay.io/frrouting/frr:9.1.2"
FRR_DAEMONS = ["zebra", "staticd", "bgpd", "ospfd", "ospf6d", "ripd", "ripngd",
               "isisd", "eigrpd", "bfdd"]

TEMPLATES = {
    "frr": {"label": "Router (FRR)", "kind": "docker", "image": FRR_IMAGE,
            "icon": "router", "category": "router", "ifaces": 4, "ram": 160, "consoles": ["vtysh", "bash"]},
    "linux": {"label": "PC (Alpine)", "kind": "docker", "image": "alpine:3.20",
              "icon": "pc", "category": "host", "ifaces": 1, "ram": 64, "consoles": ["sh"]},
    "qemu": {"label": "QEMU VM", "kind": "qemu", "icon": "server", "category": "other", "ifaces": 4,
             "ram": 1024, "cpu": 1, "consoles": ["serial"]},
}

# Known QEMU appliances, grouped by category. An image folder data/images/qemu/<key>-<version>/
# (e.g. opnsense-24.7, mikrotik-chr-7.25) picks up these defaults; its netlab.json overrides them.
# iface_names name the first ports (management), the rest are iface_prefix + number.
QEMU_CATALOG = {
    # routers
    "mikrotik": {"label": "MikroTik CHR", "category": "router", "nic": "e1000", "disk_if": "ide",
                 "ram": 256, "ifaces": 4, "iface_prefix": "ether", "iface_start": 1},
    "vyos": {"label": "VyOS", "category": "router", "ram": 512, "ifaces": 4},
    "vios": {"label": "Cisco vIOS", "category": "router", "nic": "e1000", "ram": 512, "ifaces": 4,
             "iface_prefix": "Gi0/", "iface_start": 0},
    "c8000v": {"label": "Cisco Catalyst 8000v", "category": "router", "ram": 4096, "cpu": 1, "ifaces": 4,
               "iface_prefix": "Gi", "iface_start": 1},
    "openwrt": {"label": "OpenWrt", "category": "router", "ram": 128, "ifaces": 3},
    # switches
    "viosl2": {"label": "Cisco vIOS-L2", "category": "switch", "nic": "e1000", "ram": 768, "ifaces": 8,
               "iface_prefix": "Gi0/", "iface_start": 0},
    "cumulus": {"label": "Cumulus VX", "category": "switch", "ram": 768, "ifaces": 8,
                "iface_names": ["eth0"], "iface_prefix": "swp", "iface_start": 1},
    # firewalls
    "opnsense": {"label": "OPNsense", "category": "firewall", "ram": 1024, "ifaces": 4,
                 "iface_prefix": "vtnet", "iface_start": 0},
    "pfsense": {"label": "pfSense CE", "category": "firewall", "ram": 1024, "ifaces": 4,
                "iface_prefix": "vtnet", "iface_start": 0},
    "fortigate": {"label": "FortiGate VM", "category": "firewall", "ram": 2048, "ifaces": 4,
                  "iface_prefix": "port", "iface_start": 1},
    "asav": {"label": "Cisco ASAv", "category": "firewall", "ram": 2048, "ifaces": 4,
             "iface_names": ["Management0/0"], "iface_prefix": "Gi0/", "iface_start": 0},
    "vsrx": {"label": "Juniper vSRX", "category": "firewall", "cpu": 2, "ram": 4096, "ifaces": 4,
             "iface_names": ["fxp0"], "iface_prefix": "ge-0/0/", "iface_start": 0},
    "paloalto": {"label": "Palo Alto VM-Series", "category": "firewall", "cpu": 2, "ram": 6656, "ifaces": 4,
                 "iface_names": ["mgmt"], "iface_prefix": "ethernet1/", "iface_start": 1},
    # hosts
    "linux": {"label": "Linux VM", "category": "host", "ram": 512, "ifaces": 1},
    "windows": {"label": "Windows", "category": "host", "nic": "e1000", "disk_if": "ide", "cpu": 2,
                "ram": 4096, "ifaces": 1},
}
NAME_PREFIX = {"router": "R", "switch": "SW", "firewall": "FW", "host": "PC", "other": "VM"}
RAM_RESERVE_MB = int(os.environ.get("NETLAB_RAM_RESERVE_MB", 256))

# Clouds. "nat": one shared host bridge with a gateway IP, DHCP and masquerade out of the
# server's uplink. "pnet": an existing host bridge pnetN that holds a physical NIC (EVE-NG style);
# NetLab only attaches ports to it and never creates, changes or deletes it.
NETWORK_TYPES = ("bridge", "nat", "pnet")
NAT_BRIDGE = "nlnat0"
NAT_NET = ipaddress.ip_network(os.environ.get("NETLAB_NAT_SUBNET", "10.250.250.0/24"))
NAT_GW = next(NAT_NET.hosts())

_file_lock = threading.RLock()
_net_lock = threading.RLock()


class LabError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def sh(args, check=True, capture=True):
    p = subprocess.run(args, capture_output=capture, text=True)
    if check and p.returncode != 0:
        raise LabError(f"{' '.join(args[:4])}...: {(p.stderr or p.stdout).strip()}")
    return p


def _h(*parts, n=10):
    return hashlib.sha1("/".join(map(str, parts)).encode()).hexdigest()[:n]


def port_name(lab_id, node_id, iface):
    return "nlv" + _h(lab_id, node_id, iface)


def bridge_name(lab_id, key):
    return "nlb" + _h(lab_id, key)


def container_name(lab_id, node_id):
    return f"nl-{lab_id}-{node_id}"


def node_dir(lab_id, node_id):
    return NODES / lab_id / str(node_id)


def _hostname(name):
    return (re.sub(r"[^A-Za-z0-9-]", "-", name).strip("-") or "node")[:63]


def _no_ipv6(ifname):
    # keep the host from sending its own ND/RS traffic into lab segments
    try:
        Path(f"/proc/sys/net/ipv6/conf/{ifname}/disable_ipv6").write_text("1")
    except OSError:
        pass


def _link_exists(ifname):
    return Path(f"/sys/class/net/{ifname}").exists()


def ensure_forwarding_rule():
    """Docker sets FORWARD policy DROP and br_netfilter passes bridged frames
    through iptables, which would silently drop lab traffic."""
    for tool in ("iptables", "ip6tables"):
        if not shutil.which(tool):
            continue
        chain = "DOCKER-USER" if sh([tool, "-L", "DOCKER-USER", "-n"], check=False).returncode == 0 else "FORWARD"
        rule = ["-i", "nlb+", "-o", "nlb+", "-j", "ACCEPT"]
        if sh([tool, "-C", chain] + rule, check=False).returncode != 0:
            sh([tool, "-I", chain] + rule, check=False)


# ---------------------------------------------------------------- storage

def _lab_path(lab_id):
    if not re.fullmatch(r"[0-9a-f]{8}", lab_id or ""):
        raise LabError("invalid lab id")
    return LABS / f"{lab_id}.json"


def load(lab_id):
    p = _lab_path(lab_id)
    if not p.exists():
        raise LabError("lab not found")
    return json.loads(p.read_text())


def save(lab):
    p = _lab_path(lab["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(lab, indent=2))
    tmp.replace(p)


def list_labs():
    out = []
    for p in sorted(LABS.glob("*.json")):
        lab = json.loads(p.read_text())
        out.append({"id": lab["id"], "name": lab["name"], "nodes": len(lab["nodes"])})
    return out


def create_lab(name, data=None):
    with _file_lock:
        lab = {"id": uuid.uuid4().hex[:8], "name": name or "New lab", "seq": 0,
               "nodes": {}, "networks": {}, "links": {}}
        if data:  # import
            for k in ("seq", "nodes", "networks", "links"):
                if k in data:
                    lab[k] = data[k]
        save(lab)
        return lab


def _next_id(lab):
    lab["seq"] = lab.get("seq", 0) + 1
    return str(lab["seq"])


def update_lab(lab_id, fields):
    with _file_lock:
        lab = load(lab_id)
        if "name" in fields:
            lab["name"] = str(fields["name"])[:80]
        save(lab)
        return lab


def delete_lab(lab_id):
    stop_lab(lab_id)
    with _file_lock:
        _lab_path(lab_id).unlink(missing_ok=True)
    shutil.rmtree(NODES / lab_id, ignore_errors=True)


def _catalog_key(name):
    keys = [k for k in QEMU_CATALOG if name == k or name.startswith(k + "-")]
    return max(keys, key=len) if keys else None


def image_meta(name):
    """Catalog defaults for an image folder, overridden by its netlab.json."""
    d = IMAGES / name
    meta = json.loads((d / "netlab.json").read_text()) if (d / "netlab.json").exists() else {}
    key = meta.get("template") if meta.get("template") in QEMU_CATALOG else _catalog_key(name)
    base = QEMU_CATALOG.get(key, {})
    version = name[len(key) + 1:] if key and name.startswith(key + "-") else ""
    out = {"name": name, "template": key, "category": "other", "nic": "virtio-net-pci", "disk_if": "virtio",
           "cpu": 1, "ram": TEMPLATES["qemu"]["ram"], "ifaces": TEMPLATES["qemu"]["ifaces"]}
    out.update(base)
    out["label"] = f"{base['label']} {version}".strip() if base else name
    if "huawei" in name.lower():
        out["backspace"] = "ctrl-h"  # VRP serial console erases with ^H, not DEL
    out.update(meta)
    return out


def qemu_images():
    return [image_meta(d.name) for d in sorted(IMAGES.iterdir()) if d.is_dir() and any(d.glob("*.qcow2"))]


# ---------------------------------------------------------------- image upload

UPLOADS = DATA / "uploads"  # staging area, same filesystem as IMAGES so the final move is a rename
DISK_RESERVE = int(os.environ.get("NETLAB_DISK_RESERVE_MB", "2048")) * 2**20
IMAGE_EXTS = (".qcow2", ".img", ".raw", ".vmdk", ".vhd", ".vhdx", ".vdi", ".iso")
IMAGE_FORMATS = {"qcow2", "raw", "vmdk", "vpc", "vhdx", "vdi"}
# settings a custom upload may set in netlab.json (never "extra": those are raw qemu arguments)
IMAGE_META = {"label": str, "category": str, "ram": int, "cpu": int, "ifaces": int, "nic": str,
              "disk_if": str, "iface_prefix": str, "iface_start": int, "backspace": str}
NICS = {"virtio-net-pci", "e1000", "e1000e", "vmxnet3", "rtl8139"}
DISK_IFS = {"virtio", "ide", "scsi"}
CATEGORIES = {"router", "switch", "firewall", "host", "other"}


def disk_free():
    return shutil.disk_usage(IMAGES).free


def _image_users(name):
    out = []
    for p in sorted(LABS.glob("*.json")):
        lab = json.loads(p.read_text())
        if any(n.get("image") == name for n in lab["nodes"].values()):
            out.append(lab["name"])
    return out


def list_images():
    out = []
    for m in qemu_images():
        d = IMAGES / m["name"]
        size = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
        out.append({"name": m["name"], "label": m["label"], "category": m["category"], "ram": m["ram"],
                    "size": size, "iso": (d / "cdrom.iso").exists(), "used_by": _image_users(m["name"])})
    return {"images": out, "free": disk_free(), "reserve": DISK_RESERVE}


def check_new_image(name, filename, size):
    """Validate an upload before any bytes are stored."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", name or ""):
        raise LabError("image name: lowercase letters, digits, '.', '_' and '-' only")
    if (IMAGES / name).exists():
        raise LabError(f"image {name} already exists; delete it first")
    if not str(filename).lower().endswith(IMAGE_EXTS):
        raise LabError("disk file must be one of " + ", ".join(IMAGE_EXTS))
    if size is not None and size > disk_free() - DISK_RESERVE:
        raise LabError(f"not enough disk space: the file is {size // 2**20} MB, the server has "
                       f"{disk_free() // 2**20} MB free and keeps {DISK_RESERVE // 2**20} MB in reserve")


def clean_meta(meta):
    out = {}
    for k, v in (meta or {}).items():
        if k not in IMAGE_META or v in ("", None):
            continue
        try:
            v = IMAGE_META[k](v)
        except (TypeError, ValueError):
            raise LabError(f"invalid {k}")
        out[k] = v
    if "label" in out:
        out["label"] = out["label"].strip()[:60]
    if out.get("category", "other") not in CATEGORIES:
        raise LabError("invalid category")
    if out.get("nic", "virtio-net-pci") not in NICS:
        raise LabError("invalid nic")
    if out.get("disk_if", "virtio") not in DISK_IFS:
        raise LabError("invalid disk_if")
    if not 64 <= out.get("ram", 64) <= 65536 or not 1 <= out.get("cpu", 1) <= 16 \
            or not 1 <= out.get("ifaces", 1) <= 24 or not 0 <= out.get("iface_start", 0) <= 9:
        raise LabError("ram, cpu, ifaces or iface_start out of range")
    if out.get("backspace", "del") not in ("del", "ctrl-h"):
        raise LabError("backspace must be del or ctrl-h")
    if not re.fullmatch(r"[A-Za-z/-]{0,24}", out.get("iface_prefix", "")):
        raise LabError("invalid iface_prefix")
    return out


def _disk_info(path):
    r = sh(["qemu-img", "info", "--output=json", str(path)], check=False)
    if r.returncode:
        raise LabError("not a disk image qemu can read")
    info = json.loads(r.stdout)
    fmt = info.get("format")
    if fmt not in IMAGE_FORMATS:
        raise LabError(f"unsupported disk format {fmt}")
    # a disk that points at other files could read host files into the VM
    if info.get("backing-filename") or info.get("format-specific", {}).get("data", {}).get("data-file"):
        raise LabError("disk images with a backing or external data file are not accepted")
    if fmt == "vmdk":
        with open(path, "rb") as f:
            if f.read(4) != b"KDMV":
                raise LabError("only single-file (sparse) VMDK disks are accepted")
    return fmt


def _is_iso(path):
    with open(path, "rb") as f:
        f.seek(0x8001)
        return f.read(5) == b"CD001"  # ISO 9660 volume descriptor


def install_image(tmp, name, meta, filename="", disk_gb=8):
    """Turn an uploaded disk into data/images/qemu/<name>/disk.qcow2. Consumes tmp.

    An .iso becomes cdrom.iso (read-only installer/live CD) next to an empty disk.qcow2 of disk_gb,
    like EVE-NG: the VM boots the disk first and falls back to the CD while the disk is blank.
    """
    stage = UPLOADS / ("dir-" + uuid.uuid4().hex[:8])
    try:
        iso = str(filename).lower().endswith(".iso")
        if iso and not _is_iso(tmp):
            raise LabError("the .iso file is not an ISO 9660 image")
        if not 1 <= int(disk_gb) <= 512:
            raise LabError("disk size must be 1 to 512 GB")
        fmt = "iso" if iso else _disk_info(tmp)
        stage.mkdir(parents=True)
        disk = stage / "disk.qcow2"
        if fmt == "iso":
            tmp.rename(stage / "cdrom.iso")
            sh(["qemu-img", "create", "-f", "qcow2", str(disk), f"{int(disk_gb)}G"])
        elif fmt == "qcow2":
            tmp.rename(disk)
        else:
            r = sh(["qemu-img", "convert", "-f", fmt, "-O", "qcow2", str(tmp), str(disk)], check=False)
            if r.returncode:
                raise LabError("converting to qcow2 failed: " + (r.stderr or "").strip()[-300:])
        if meta:
            (stage / "netlab.json").write_text(json.dumps(meta, indent=1))
        with _file_lock:
            if (IMAGES / name).exists():
                raise LabError(f"image {name} already exists")
            stage.rename(IMAGES / name)
        return image_meta(name)
    finally:
        tmp.unlink(missing_ok=True)
        shutil.rmtree(stage, ignore_errors=True)


def delete_image(name):
    # leading [a-z0-9] rules out "." and ".." (which would point at IMAGES or its parent)
    if not re.fullmatch(r"[A-Za-z0-9][\w.-]*", name) or not (IMAGES / name).is_dir():
        raise LabError("no such image")
    users = _image_users(name)
    if users:
        raise LabError("image is used by lab " + ", ".join(users) + "; delete those nodes first")
    shutil.rmtree(IMAGES / name)


def templates():
    catalog = [{"key": k, "label": v["label"], "category": v["category"], "ram": v["ram"]}
               for k, v in QEMU_CATALOG.items()]
    return {"templates": TEMPLATES, "qemu_images": qemu_images(), "qemu_catalog": catalog, "clouds": clouds(),
            "qemu_available": bool(shutil.which("qemu-system-x86_64"))}


def _unique_name(lab, prefix):
    used = {o["name"] for o in list(lab["nodes"].values()) + list(lab["networks"].values())}
    n = 1
    while f"{prefix}{n}" in used:
        n += 1
    return f"{prefix}{n}"


def _mem_available_mb():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return 0


# ---------------------------------------------------------------- topology edits

def add_node(lab_id, spec):
    tpl_key = spec.get("template")
    if tpl_key not in TEMPLATES:
        raise LabError("unknown template")
    tpl = TEMPLATES[tpl_key]
    with _file_lock:
        lab = load(lab_id)
        defaults, image = tpl, None
        if tpl["kind"] == "qemu":
            image = spec.get("image") or ""
            if not re.fullmatch(r"[A-Za-z0-9][\w.-]*", image) or not (IMAGES / image).is_dir():
                raise LabError("pick a QEMU image (data/images/qemu/<name>/)")
            defaults = image_meta(image)
        nid = _next_id(lab)
        node = {"id": nid, "template": tpl_key,
                "name": spec.get("name") or _unique_name(lab, NAME_PREFIX.get(defaults["category"], "VM")),
                "x": int(spec.get("x", 100)), "y": int(spec.get("y", 100)),
                "ifaces": int(spec.get("ifaces", defaults["ifaces"])),
                "ram": int(spec.get("ram", defaults["ram"]))}
        if image:
            node["image"] = image
            node["cpu"] = int(spec.get("cpu", defaults["cpu"]))
        if spec.get("config"):
            node["config"] = spec["config"]
        lab["nodes"][nid] = node
        save(lab)
        return node


def update_node(lab_id, node_id, fields):
    with _file_lock:
        lab = load(lab_id)
        node = lab["nodes"].get(node_id) or _raise("node not found")
        if "ifaces" in fields and int(fields["ifaces"]) != node["ifaces"]:
            if node_state(lab_id, node) == "running":
                raise LabError("stop the node before changing interface count")
            new = max(1, min(16, int(fields["ifaces"])))
            used = [e["iface"] for e in _node_ends(lab, node_id)]
            if used and max(used) >= new:
                raise LabError("an interface above that number is still connected")
            node["ifaces"] = new
        for k in ("name", "x", "y", "ram", "cpu"):
            if k in fields:
                node[k] = fields[k] if k == "name" else int(fields[k])
        save(lab)
        return node


def delete_node(lab_id, node_id):
    stop_node(lab_id, node_id)
    with _file_lock:
        lab = load(lab_id)
        for lid in [lid for lid, l in lab["links"].items()
                    if any(e.get("node") == node_id for e in (l["a"], l["b"]))]:
            _drop_link(lab, lid)
        lab["nodes"].pop(node_id, None)
        save(lab)
    shutil.rmtree(node_dir(lab_id, node_id), ignore_errors=True)


def add_network(lab_id, spec):
    with _file_lock:
        lab = load(lab_id)
        ntype = spec.get("type") or "bridge"
        if ntype not in NETWORK_TYPES:
            raise LabError("unknown network type")
        nid = _next_id(lab)
        prefix = {"bridge": "SW", "nat": "NAT", "pnet": "Cloud"}[ntype]
        net = {"id": nid, "type": ntype, "name": spec.get("name") or _unique_name(lab, prefix),
               "x": int(spec.get("x", 100)), "y": int(spec.get("y", 100))}
        if ntype == "pnet":
            br = spec.get("bridge") or ""
            if br not in {b["bridge"] for b in host_bridges()}:
                raise LabError(f"host bridge {br or 'pnet0'} is not set up on the server")
            net["bridge"] = br
        lab["networks"][nid] = net
        save(lab)
        return net


def update_network(lab_id, net_id, fields):
    with _file_lock:
        lab = load(lab_id)
        net = lab["networks"].get(net_id) or _raise("network not found")
        for k in ("name", "x", "y"):
            if k in fields:
                net[k] = fields[k] if k == "name" else int(fields[k])
        save(lab)
        return net


def delete_network(lab_id, net_id):
    with _file_lock:
        lab = load(lab_id)
        for lid in [lid for lid, l in lab["links"].items() if l["b"].get("network") == net_id]:
            _drop_link(lab, lid)
        net = lab["networks"].pop(net_id, None)
        save(lab)
    if net and net.get("type", "bridge") == "bridge":  # clouds are shared host bridges
        with _net_lock:
            _del_bridge(bridge_name(lab_id, "n" + net_id))


def _node_ends(lab, node_id):
    return [e for l in lab["links"].values() for e in (l["a"], l["b"]) if e.get("node") == node_id]


def _free_iface(lab, node_id):
    used = {e["iface"] for e in _node_ends(lab, node_id)}
    for i in range(lab["nodes"][node_id]["ifaces"]):
        if i not in used:
            return i
    raise LabError(f"{lab['nodes'][node_id]['name']} has no free interface")


def add_link(lab_id, spec):
    with _file_lock:
        lab = load(lab_id)
        ends = []
        for key in ("a", "b"):
            e = dict(spec.get(key) or {})
            if e.get("network"):
                if key == "a" or e["network"] not in lab["networks"]:
                    raise LabError("network must exist and be the 'b' end")
                ends.append({"network": e["network"]})
                continue
            if e.get("node") not in lab["nodes"]:
                raise LabError("node not found")
            if e.get("iface") is None:
                e["iface"] = _free_iface(lab, e["node"])
            e["iface"] = int(e["iface"])
            if e["iface"] >= lab["nodes"][e["node"]]["ifaces"]:
                raise LabError("interface out of range")
            if any(x["iface"] == e["iface"] for x in _node_ends(lab, e["node"])):
                raise LabError("interface already connected")
            ends.append({"node": e["node"], "iface": e["iface"]})
        if ends[0].get("node") == ends[1].get("node"):
            raise LabError("cannot link a node to itself")
        lid = _next_id(lab)
        link = {"id": lid, "a": ends[0], "b": ends[1]}
        lab["links"][lid] = link
        save(lab)
    _wire_link(lab, link)
    return link


def delete_link(lab_id, link_id):
    with _file_lock:
        lab = load(lab_id)
        _drop_link(lab, link_id)
        save(lab)


def _drop_link(lab, link_id):
    link = lab["links"].pop(link_id, None)
    if not link:
        return
    with _net_lock:
        if link["b"].get("network"):
            p = port_name(lab["id"], link["a"]["node"], link["a"]["iface"])
            if _link_exists(p):
                sh(["ip", "link", "set", p, "nomaster"], check=False)
        else:
            _del_bridge(bridge_name(lab["id"], "l" + link_id))


def _raise(msg):
    raise LabError(msg)


# ---------------------------------------------------------------- wiring

def _link_net(lab, link):
    return lab["networks"].get(link["b"].get("network") or "")


def _link_bridge(lab, link):
    net = _link_net(lab, link)
    if not net:
        return bridge_name(lab["id"], "l" + link["id"])
    ntype = net.get("type", "bridge")
    if ntype == "nat":
        return NAT_BRIDGE
    if ntype == "pnet":
        return net["bridge"]
    return bridge_name(lab["id"], "n" + net["id"])


def _prepare_bridge(lab, link):
    br = _link_bridge(lab, link)
    ntype = (_link_net(lab, link) or {}).get("type", "bridge")
    if ntype == "nat":
        ensure_nat()
    elif ntype == "pnet":
        if not Path(f"/sys/class/net/{br}/bridge").exists():
            raise LabError(f"host bridge {br} does not exist on the server")
    else:
        _ensure_bridge(br)
    return br


def _ensure_bridge(br):
    if not _link_exists(br):
        sh(["ip", "link", "add", br, "type", "bridge", "stp_state", "0", "mcast_snooping", "0"])
        _no_ipv6(br)
        # forward LLDP/LACP-style link-local frames so the segment acts like a wire
        sh(["ip", "link", "set", br, "type", "bridge", "group_fwd_mask", "0x4000"], check=False)
    sh(["ip", "link", "set", br, "up"])


def _del_bridge(br):
    if _link_exists(br):
        sh(["ip", "link", "del", br], check=False)


def _wire_link(lab, link):
    with _net_lock:
        ports = [port_name(lab["id"], e["node"], e["iface"]) for e in (link["a"], link["b"]) if e.get("node")]
        ports = [p for p in ports if _link_exists(p)]
        if not ports:
            return
        br = _prepare_bridge(lab, link)
        for p in ports:
            sh(["ip", "link", "set", p, "master", br, "up"])


def _wire_node(lab, node_id):
    for link in lab["links"].values():
        if any(e.get("node") == node_id for e in (link["a"], link["b"])):
            _wire_link(lab, link)


# ---------------------------------------------------------------- clouds

def host_bridges():
    """Host bridges named pnetN (EVE-NG convention) that a cloud can attach to."""
    out = []
    for p in sorted(Path("/sys/class/net").glob("pnet*")):
        if re.fullmatch(r"pnet\d+", p.name) and (p / "bridge").exists():
            addr = sh(["ip", "-4", "-br", "addr", "show", p.name], check=False).stdout.split()[2:]
            out.append({"bridge": p.name, "addresses": addr, "members": sorted(x.name for x in (p / "brif").iterdir())})
    return out


def _uplink():
    m = re.search(r"\bdev (\S+)", sh(["ip", "-4", "route", "show", "default"], check=False).stdout)
    return m.group(1) if m else None


def _fwd_chain():
    return "DOCKER-USER" if sh(["iptables", "-L", "DOCKER-USER", "-n"], check=False).returncode == 0 else "FORWARD"


def _ipt(rule, chain=None, table="filter"):
    chain = chain or _fwd_chain()
    if sh(["iptables", "-t", table, "-C", chain] + rule, check=False).returncode != 0:
        sh(["iptables", "-t", table, "-I", chain] + rule)


def _drop_stale_nat_rules(uplink):
    """Remove NAT-cloud rules written for an earlier uplink (e.g. ens18 before it joined pnet0)."""
    for table, chain in (("filter", _fwd_chain()), ("nat", "POSTROUTING")):
        for line in sh(["iptables", "-t", table, "-S", chain], check=False).stdout.splitlines():
            if NAT_BRIDGE not in line and str(NAT_NET) not in line:
                continue
            outs = re.findall(r"-o (\S+)", line)
            if any(o not in (uplink, NAT_BRIDGE) for o in outs):
                sh(["iptables", "-t", table, "-D"] + shlex.split(line)[1:], check=False)


def _upstream_dns():
    for f in ("/run/systemd/resolve/resolv.conf", "/etc/resolv.conf"):
        try:
            for line in Path(f).read_text().splitlines():
                parts = line.split()
                if len(parts) > 1 and parts[0] == "nameserver" and not parts[1].startswith("127."):
                    return parts[1]
        except OSError:
            pass
    return "1.1.1.1"


def ensure_nat():
    """Shared NAT cloud: gateway on nlnat0, DHCP, masquerade out of the default-route interface.
    Lab traffic may only leave through the uplink, so it cannot reach Docker networks on this host."""
    with _net_lock:
        if not _link_exists(NAT_BRIDGE):
            sh(["ip", "link", "add", NAT_BRIDGE, "type", "bridge", "stp_state", "0"])
            _no_ipv6(NAT_BRIDGE)
        gw = f"{NAT_GW}/{NAT_NET.prefixlen}"
        if gw not in sh(["ip", "-4", "-br", "addr", "show", NAT_BRIDGE]).stdout:
            sh(["ip", "addr", "add", gw, "dev", NAT_BRIDGE])
        sh(["ip", "link", "set", NAT_BRIDGE, "up"])
        uplink = _uplink() or _raise("server has no default route for the NAT cloud")
        _drop_stale_nat_rules(uplink)
        _ipt(["-s", str(NAT_NET), "-o", uplink, "-j", "MASQUERADE"], chain="POSTROUTING", table="nat")
        _ipt(["-o", NAT_BRIDGE, "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"])
        _ipt(["-i", NAT_BRIDGE, "-o", uplink, "-j", "ACCEPT"])
        # anything else routed from the lab (Docker published ports, Docker networks) is dropped;
        # the ACCEPT for traffic inside the cloud is inserted after, so it sits above the DROP
        _ipt(["-i", NAT_BRIDGE, "!", "-o", uplink, "-j", "DROP"])
        intra = ["-i", NAT_BRIDGE, "-o", NAT_BRIDGE, "-j", "ACCEPT"]
        sh(["iptables", "-D", _fwd_chain()] + intra, check=False)  # re-insert so it stays above the DROP
        sh(["iptables", "-I", _fwd_chain()] + intra)
        _ensure_nat_dhcp()


def _ensure_nat_dhcp():
    pidf = DATA / "nat-dnsmasq.pid"
    try:
        pid = int(pidf.read_text())
        if b"dnsmasq" in Path(f"/proc/{pid}/cmdline").read_bytes():
            return
    except (OSError, ValueError):
        pass
    if not shutil.which("dnsmasq"):
        return
    hosts = list(NAT_NET.hosts())
    sh(["dnsmasq", "--conf-file=/dev/null", "--port=0", "--user=root", "--bind-interfaces",
        f"--interface={NAT_BRIDGE}", "--except-interface=lo",
        f"--dhcp-range={hosts[len(hosts) // 2]},{hosts[-1]},12h",
        f"--dhcp-option=option:router,{NAT_GW}", f"--dhcp-option=option:dns-server,{_upstream_dns()}",
        f"--pid-file={pidf}", f"--dhcp-leasefile={DATA / 'nat-dnsmasq.leases'}",
        f"--log-facility={DATA / 'nat-dnsmasq.log'}", "--log-dhcp"])


def clouds():
    hosts = list(NAT_NET.hosts())
    return {"nat": {"subnet": str(NAT_NET), "gateway": str(NAT_GW),
                    "dhcp": f"{hosts[len(hosts) // 2]} - {hosts[-1]}" if shutil.which("dnsmasq") else None},
            "host_bridges": host_bridges()}


# ---------------------------------------------------------------- state

def node_state(lab_id, node):
    if TEMPLATES[node["template"]]["kind"] == "docker":
        p = sh(["docker", "inspect", "-f", "{{.State.Status}}",
                container_name(lab_id, node["id"])], check=False)
        return "running" if p.stdout.strip() == "running" else "stopped"
    return "running" if _qemu_pid(lab_id, node["id"]) else "stopped"


def lab_status(lab_id):
    lab = load(lab_id)
    p = sh(["docker", "ps", "--filter", f"label=netlab.lab={lab_id}",
            "--format", '{{.Label "netlab.node"}}'], check=False)
    running = set(p.stdout.split())
    out = {}
    for nid, node in lab["nodes"].items():
        if TEMPLATES[node["template"]]["kind"] == "docker":
            out[nid] = "running" if nid in running else "stopped"
        else:
            out[nid] = "running" if _qemu_pid(lab_id, nid) else "stopped"
    return out


def _port_carrier(port):
    try:
        return Path(f"/sys/class/net/{port}/carrier").read_text().strip() == "1"
    except OSError:  # missing, or administratively down
        return False


def _port_master(port):
    try:
        return os.path.basename(os.readlink(f"/sys/class/net/{port}/master"))
    except OSError:
        return None


def link_status(lab_id):
    """Per link: "up" when every device port on it has carrier and sits on the link's bridge.
    A veth loses carrier when the node's own interface is shut down, so this tracks the guest too."""
    lab = load(lab_id)
    out = {}
    for lid, link in lab["links"].items():
        br = _link_bridge(lab, link)
        ports = [port_name(lab_id, e["node"], e["iface"]) for e in (link["a"], link["b"]) if e.get("node")]
        out[lid] = "up" if all(_port_carrier(p) and _port_master(p) == br for p in ports) else "down"
    return out


# ---------------------------------------------------------------- start / stop

def start_node(lab_id, node_id):
    lab = load(lab_id)
    node = lab["nodes"].get(node_id) or _raise("node not found")
    if node_state(lab_id, node) == "running":
        return
    kind = TEMPLATES[node["template"]]["kind"]
    with _net_lock:
        ensure_forwarding_rule()
    if kind == "docker":
        _docker_start(lab, node)
    else:
        _qemu_start(lab, node)
    with _net_lock:
        _wire_node(lab, node_id)


def stop_node(lab_id, node_id):
    lab = load(lab_id)
    node = lab["nodes"].get(node_id)
    if not node:
        return
    if TEMPLATES[node["template"]]["kind"] == "docker":
        sh(["docker", "rm", "-f", container_name(lab_id, node_id)], check=False)
    else:
        _qemu_stop(lab_id, node)


def wipe_node(lab_id, node_id):
    """Back to the initial config, like EVE-NG's wipe."""
    stop_node(lab_id, node_id)
    shutil.rmtree(node_dir(lab_id, node_id), ignore_errors=True)


def start_lab(lab_id):
    errors = {}
    for nid in load(lab_id)["nodes"]:
        try:
            start_node(lab_id, nid)
        except LabError as e:
            errors[nid] = str(e)
    return errors


def stop_lab(lab_id):
    lab = load(lab_id)
    for nid in lab["nodes"]:
        stop_node(lab_id, nid)
    with _net_lock:  # remove this lab's own bridges; shared clouds stay
        for link in lab["links"].values():
            if not _link_net(lab, link):
                _del_bridge(bridge_name(lab_id, "l" + link["id"]))
        for net_id, net in lab["networks"].items():
            if net.get("type", "bridge") == "bridge":
                _del_bridge(bridge_name(lab_id, "n" + net_id))


# ---------------------------------------------------------------- docker driver

def _frr_seed(fdir, node):
    fdir.mkdir(parents=True, exist_ok=True)
    daemons = fdir / "daemons"
    if not daemons.exists():
        base = sh(["docker", "run", "--rm", "--entrypoint", "cat", FRR_IMAGE, "/etc/frr/daemons"]).stdout
        for d in FRR_DAEMONS:
            base = re.sub(rf"^{d}=no", f"{d}=yes", base, flags=re.M)
        daemons.write_text(base)
    if not (fdir / "vtysh.conf").exists():
        (fdir / "vtysh.conf").write_text("service integrated-vtysh-config\n")
    if not (fdir / "frr.conf").exists():
        body = node.get("config") or ""
        (fdir / "frr.conf").write_text(
            f"frr defaults traditional\nhostname {_hostname(node['name'])}\n"
            f"service integrated-vtysh-config\n!\n{body}\n!\nline vty\n!\n")


def _docker_start(lab, node):
    lab_id, nid = lab["id"], node["id"]
    tpl = TEMPLATES[node["template"]]
    cname = container_name(lab_id, nid)
    ndir = node_dir(lab_id, nid)
    ndir.mkdir(parents=True, exist_ok=True)
    sh(["docker", "rm", "-f", cname], check=False)
    # the entrypoint waits for /tmp/.nl-ready so daemons start after eth* exist
    wait = "while [ ! -e /tmp/.nl-ready ]; do sleep 0.2; done; "
    args = ["docker", "run", "-d", "--name", cname, "--hostname", _hostname(node["name"]),
            "--network", "none", "--label", f"netlab.lab={lab_id}", "--label", f"netlab.node={nid}",
            "--memory", f"{node.get('ram', tpl['ram'])}m",
            "--cap-add", "NET_ADMIN", "--cap-add", "NET_RAW",
            "--sysctl", "net.ipv4.ip_forward=1", "--sysctl", "net.ipv6.conf.all.forwarding=1"]
    if node["template"] == "frr":
        _frr_seed(ndir / "frr", node)
        args += ["--cap-add", "SYS_ADMIN", "-v", f"{ndir / 'frr'}:/etc/frr",
                 "--entrypoint", "/sbin/tini", tpl["image"], "--", "bash", "-c",
                 wait + "chown -R frr:frr /etc/frr; exec /usr/lib/frr/docker-start"]
    else:
        startup = ndir / "startup.sh"
        if not startup.exists():
            startup.write_text((node.get("config") or "# runs at boot\n") + "\n")
        args += ["--init", "-v", f"{ndir}:/netlab", tpl["image"], "sh", "-c",
                 wait + "sh /netlab/startup.sh; exec tail -f /dev/null"]
    sh(args)
    pid = sh(["docker", "inspect", "-f", "{{.State.Pid}}", cname]).stdout.strip()
    try:
        with _net_lock:
            for i in range(node["ifaces"]):
                host = port_name(lab_id, nid, i)
                if _link_exists(host):
                    sh(["ip", "link", "del", host], check=False)
                sh(["ip", "link", "add", host, "type", "veth", "peer", "name", f"eth{i}", "netns", pid])
                _no_ipv6(host)
                sh(["ip", "link", "set", host, "up"])
                sh(["nsenter", "-t", pid, "-n", "ip", "link", "set", f"eth{i}", "up"])
        sh(["docker", "exec", cname, "touch", "/tmp/.nl-ready"])
    except LabError:
        sh(["docker", "rm", "-f", cname], check=False)
        raise


# ---------------------------------------------------------------- qemu driver

def _qemu_state_file(lab_id, nid):
    return node_dir(lab_id, nid) / "qemu.json"


def _qemu_runtime(lab_id, nid):
    f = _qemu_state_file(lab_id, nid)
    return json.loads(f.read_text()) if f.exists() else None


def _qemu_pid(lab_id, nid):
    st = _qemu_runtime(lab_id, nid)
    if not st:
        return None
    try:
        try:
            os.kill(st["pid"], 0)
        except PermissionError:  # it exists; the caller just may not signal it
            pass
        cmdline = Path(f"/proc/{st['pid']}/cmdline").read_bytes()
        return st["pid"] if container_name(lab_id, nid).encode() in cmdline else None
    except (OSError, KeyError):
        return None


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _qemu_start(lab, node):
    qemu = shutil.which("qemu-system-x86_64") or _raise(
        "qemu-system-x86_64 not installed (apt install qemu-system-x86 qemu-utils)")
    lab_id, nid = lab["id"], node["id"]
    img_dir = IMAGES / node["image"]
    disks = sorted(img_dir.glob("*.qcow2")) or _raise("image has no .qcow2 disk")
    meta = image_meta(node["image"])
    need, avail = int(node.get("ram", meta["ram"])), _mem_available_mb()
    if need > avail - RAM_RESERVE_MB:
        raise LabError(f"not enough free memory for {node['name']}: needs {need} MB, server has {avail} MB "
                       f"available and keeps {RAM_RESERVE_MB} MB in reserve")
    ndir = node_dir(lab_id, nid)
    ndir.mkdir(parents=True, exist_ok=True)
    drives = []
    for i, base in enumerate(disks):  # copy-on-write overlay per node, base stays untouched
        ov = ndir / base.name
        if not ov.exists():
            sh(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(base), str(ov)])
        drives += ["-drive", f"file={ov},if={meta.get('disk_if', 'virtio')},index={i}"]
    iso = img_dir / "cdrom.iso"
    if iso.exists():  # shared read-only; boot the disk, fall back to the CD while the disk is blank
        drives += ["-drive", f"file={iso},format=raw,media=cdrom,readonly=on,if=ide,index=2", "-boot", "order=cd"]
    port = _free_port()
    nic = meta.get("nic", "virtio-net-pci")
    args = [qemu, "-name", container_name(lab_id, nid), "-m", str(node.get("ram", 1024)),
            "-smp", str(node.get("cpu", 1)), "-display", "none", "-monitor", "none",
            "-serial", f"telnet:127.0.0.1:{port},server,nowait"] + drives
    if Path("/dev/kvm").exists():
        args += ["-enable-kvm", "-cpu", "host"]
    with _net_lock:
        for i in range(node["ifaces"]):
            tap = port_name(lab_id, nid, i)
            if _link_exists(tap):
                sh(["ip", "link", "del", tap], check=False)
            sh(["ip", "tuntap", "add", "dev", tap, "mode", "tap"])
            _no_ipv6(tap)
            sh(["ip", "link", "set", tap, "up"])
            mac = "50:00:" + ":".join(_h(lab_id, nid, i, n=8)[j:j + 2] for j in range(0, 8, 2))
            args += ["-netdev", f"tap,id=n{i},ifname={tap},script=no,downscript=no",
                     "-device", f"{nic},netdev=n{i},mac={mac}"]
    args += meta.get("extra", [])
    log = open(ndir / "qemu.log", "ab")
    proc = subprocess.Popen(args, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    time.sleep(0.5)
    if proc.poll() is not None:
        _qemu_del_taps(lab_id, node)
        raise LabError("qemu exited at start; see " + str(ndir / "qemu.log"))
    _qemu_state_file(lab_id, nid).write_text(json.dumps({"pid": proc.pid, "port": port}))


def _qemu_del_taps(lab_id, node):
    with _net_lock:
        for i in range(node["ifaces"]):
            tap = port_name(lab_id, node["id"], i)
            if _link_exists(tap):
                sh(["ip", "link", "del", tap], check=False)


def _qemu_stop(lab_id, node):
    pid = _qemu_pid(lab_id, node["id"])
    if pid:
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            if not _qemu_pid(lab_id, node["id"]):
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
    _qemu_state_file(lab_id, node["id"]).unlink(missing_ok=True)
    _qemu_del_taps(lab_id, node)


# ---------------------------------------------------------------- startup config

def _config_path(lab_id, node):
    d = node_dir(lab_id, node["id"])
    return {"frr": d / "frr" / "frr.conf", "linux": d / "startup.sh"}.get(node["template"])


def get_config(lab_id, node_id):
    node = load(lab_id)["nodes"].get(node_id) or _raise("node not found")
    path = _config_path(lab_id, node) or _raise("this node type has no startup config")
    if path.exists():
        return path.read_text()
    if node["template"] == "frr":
        return (f"frr defaults traditional\nhostname {_hostname(node['name'])}\n"
                f"service integrated-vtysh-config\n!\n{node.get('config') or ''}\n!\nline vty\n!\n")
    return (node.get("config") or "# runs at boot\n") + "\n"


def set_config(lab_id, node_id, text):
    node = load(lab_id)["nodes"].get(node_id) or _raise("node not found")
    path = _config_path(lab_id, node) or _raise("this node type has no startup config")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# ---------------------------------------------------------------- consoles

def strip_telnet(buf: bytes, state: dict) -> bytes:
    """Drop telnet IAC negotiation coming from QEMU's telnet serial."""
    out = bytearray()
    for b in buf:
        s = state.get("s", 0)
        if s == 0:
            if b == 255:
                state["s"] = 1
            else:
                out.append(b)
        elif s == 1:
            state["s"] = 2 if b in (251, 252, 253, 254) else (3 if b == 250 else 0)
            if b == 255:
                out.append(255)
        elif s == 2:
            state["s"] = 0
        elif s == 3 and b == 240:  # end of sub-negotiation
            state["s"] = 0
    return bytes(out)


def exec_node(lab_id, node_id, lines, shell=False):
    """Run commands in a container node: vtysh lines on FRR (unless shell), else one sh script."""
    lab = load(lab_id)
    node = lab["nodes"].get(node_id) or _raise("node not found")
    tpl = TEMPLATES[node["template"]]
    if tpl["kind"] != "docker":
        raise LabError("exec works on container nodes; VMs are reached through their console")
    if node_state(lab_id, node) != "running":
        raise LabError(f"{node['name']} is not running")
    if "vtysh" in tpl["consoles"] and not shell:
        argv = ["vtysh"] + [a for line in lines for a in ("-c", line)]
    else:
        argv = ["sh", "-c", "\n".join(lines)]
    try:
        r = subprocess.run(["docker", "exec", container_name(lab_id, node_id)] + argv,
                           capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise LabError("the command did not finish within 120 s")
    return {"output": r.stdout + r.stderr, "code": r.returncode}


def console_target(lab_id, node_id, kind):
    lab = load(lab_id)
    node = lab["nodes"].get(node_id) or _raise("node not found")
    if node_state(lab_id, node) != "running":
        raise LabError("node is not running")
    tpl = TEMPLATES[node["template"]]
    kind = kind if kind in tpl["consoles"] else tpl["consoles"][0]
    if tpl["kind"] == "qemu":
        return ("tcp", _qemu_runtime(lab_id, node_id)["port"])
    cmd = {"vtysh": ["vtysh"], "bash": ["bash"], "sh": ["sh", "-l"]}[kind]
    return ("pty", ["docker", "exec", "-it", "-e", "TERM=xterm-256color",
                    container_name(lab_id, node_id)] + cmd)


# ---------------------------------------------------------------- demo lab

def create_demo_lab():
    lab = create_lab("Demo OSPF")
    lid = lab["id"]

    def r(name, x, y, rid, ifs):
        cfg = "".join(f"interface eth{i}\n ip address {ip}\n ip ospf area 0\nexit\n!\n" for i, ip in ifs)
        cfg += f"router ospf\n ospf router-id {rid}\nexit\n"
        return add_node(lid, {"template": "frr", "name": name, "x": x, "y": y, "config": cfg})["id"]

    def pc(name, x, y, ip, gw):
        cfg = f"ip addr add {ip} dev eth0\nip route add default via {gw}\n"
        return add_node(lid, {"template": "linux", "name": name, "x": x, "y": y, "config": cfg})["id"]

    r1 = r("R1", 260, 160, "1.1.1.1", [(0, "10.0.12.1/24"), (1, "10.0.13.1/24"), (2, "192.168.1.1/24")])
    r2 = r("R2", 520, 90, "2.2.2.2", [(0, "10.0.12.2/24"), (1, "10.0.23.2/24")])
    r3 = r("R3", 520, 300, "3.3.3.3", [(0, "10.0.23.3/24"), (1, "10.0.13.3/24"), (2, "192.168.3.1/24")])
    p1 = pc("PC1", 80, 300, "192.168.1.10/24", "192.168.1.1")
    p2 = pc("PC2", 760, 300, "192.168.3.10/24", "192.168.3.1")
    for a, ai, b, bi in [(r1, 0, r2, 0), (r2, 1, r3, 0), (r1, 1, r3, 1), (r1, 2, p1, 0), (r3, 2, p2, 0)]:
        add_link(lid, {"a": {"node": a, "iface": ai}, "b": {"node": b, "iface": bi}})
    return load(lid)
