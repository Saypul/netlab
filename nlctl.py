"""nlctl: command-line access to one NetLab lab, used by the AI assistant (lab id from $NETLAB_LAB).

Everything goes through the NetLab server on 127.0.0.1, so an agent that runs its commands in a
sandbox only needs local network access: no Docker, process or data-directory access of its own.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.sync.client import connect

import auth

PORT = os.environ.get("NETLAB_PORT", "8765")
MORE = re.compile(rb"-+ ?\(?more\b[^\r\n]*$|--more--\s*$", re.I)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]|\r")
SANDBOX_HINT = "(an agent sandbox must allow network access to 127.0.0.1)"
TOKEN = None


def _token():
    global TOKEN
    if not TOKEN:
        users = auth._load()
        user = os.environ.get("NETLAB_USER")
        TOKEN = auth.issue(user if user in users else next(iter(users)))[0]
    return TOKEN


def api(method, path, body=None, timeout=150):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api{path}", method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"authorization": "Bearer " + _token(), "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except ValueError:
            detail = None
        sys.exit(detail or f"NetLab server: HTTP {e.code}")
    except OSError as e:
        sys.exit(f"cannot reach the NetLab server on 127.0.0.1:{PORT}: {e} {SANDBOX_HINT}")


def iface_name(node, i, images):
    meta = images.get(node.get("image")) or {}
    named, prefix, start = meta.get("iface_names", []), meta.get("iface_prefix", "eth"), meta.get("iface_start", 0)
    return named[i] if i < len(named) else f"{prefix}{i - len(named) + start}"


def find(lab, ref):
    for n in lab["nodes"].values():
        if ref in (n["id"], n["name"]) or ref.lower() == n["name"].lower():
            return n
    sys.exit(f"no node named {ref!r}; see `nlctl lab`")


def show(lab, tpl):
    images = {m["name"]: m for m in tpl["qemu_images"]}
    status, links = lab.get("status", {}), lab.get("link_status", {})

    def end(e):
        if e.get("network"):
            return lab["networks"].get(e["network"], {}).get("name", "?")
        n = lab["nodes"].get(e["node"], {})
        return f"{n.get('name', '?')} {iface_name(n, e['iface'], images)}"

    print(f"Lab {lab['name']} ({lab['id']})\n\nNodes:")
    for nid, n in lab["nodes"].items():
        kind = n.get("image") or tpl["templates"][n["template"]]["label"]
        ifs = ", ".join(iface_name(n, i, images) for i in range(n["ifaces"]))
        print(f"  {n['name']:<10} {status.get(nid, '?'):<8} {kind}  [{ifs}]")
    if lab["networks"]:
        print("\nSwitches / clouds:")
        for net in lab["networks"].values():
            print(f"  {net['name']}  ({net.get('type', 'bridge')})")
    print("\nCables:")
    for lid, l in lab["links"].items():
        print(f"  {end(l['a'])}  <->  {end(l['b'])}   {links.get(lid, '?')}")
    if not lab["links"]:
        print("  (none)")


def serial(lab_id, node_id, lines, wait):
    """Type lines into a VM serial console through the NetLab server, which shares it with the user's
    console tabs; return what the console printed until it goes quiet."""
    url = f"ws://127.0.0.1:{PORT}/ws/console/{lab_id}/{node_id}?ai=1&token={_token()}"
    try:
        ws = connect(url, open_timeout=5, max_size=None)
    except (OSError, InvalidHandshake) as e:
        sys.exit(f"cannot reach the NetLab server on 127.0.0.1:{PORT}: {e} {SANDBOX_HINT}")
    # one node at a time per lab: the server says "ready" when it is this command's turn
    try:
        first = ws.recv(timeout=300)
    except TimeoutError:
        sys.exit("timed out waiting for another nlctl command in this lab to finish")
    except ConnectionClosed:
        first = b""
    out = bytearray()
    if first != "ready":
        text = first.decode(errors="replace") if isinstance(first, bytes) else first
        sys.exit(text.strip(" \r\n*") or "the console closed")

    def send(text):
        ws.send(json.dumps({"t": "i", "d": text}))

    def read(quiet, limit):
        end, last = time.time() + limit, time.time()
        while time.time() < end and time.time() - last < quiet:
            try:
                data = ws.recv(timeout=0.3)
            except TimeoutError:
                continue
            except ConnectionClosed:
                break
            out.extend(data if isinstance(data, bytes) else data.encode())
            last = time.time()
            if MORE.search(bytes(out[-80:])):
                send(" ")

    read(0.3, 1)  # whatever the console prints meanwhile
    for line in lines:
        send(line + "\r")
        read(0.6, 3)
    read(wait, max(wait * 4, 20))
    ws.close()
    text = out.decode(errors="replace")
    if text.lstrip().startswith("***") and len(text) < 300:  # the server refused, e.g. console unavailable
        sys.exit(text.strip(" \r\n*"))
    return text


def run(lab, tpl, node, lines, wait, shell):
    if lab.get("status", {}).get(node["id"]) != "running":
        sys.exit(f"{node['name']} is not running; start it with `nlctl start {node['name']}`")
    if tpl["templates"][node["template"]]["kind"] == "qemu":
        print(ANSI.sub("", serial(lab["id"], node["id"], lines, wait)))
        return
    r = api("POST", f"/labs/{lab['id']}/nodes/{node['id']}/exec", {"lines": lines, "shell": shell, "ai": True}, 400)
    sys.stdout.write(r["output"])
    sys.exit(r["code"])


def main():
    p = argparse.ArgumentParser(prog="nlctl", description="Work with the current NetLab lab.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("lab", help="show nodes, interfaces and cables")
    r = sub.add_parser("run", help="run commands on a node (one argument per line)")
    r.add_argument("node")
    r.add_argument("lines", nargs="+")
    r.add_argument("--wait", type=float, default=4, help="seconds of silence that end the output (VMs)")
    r.add_argument("--shell", action="store_true", help="FRR: run a shell command instead of vtysh")
    for c in ("start", "stop"):
        sub.add_parser(c, help=f"{c} a node").add_argument("node")
    a = p.parse_args()
    lab_id = os.environ.get("NETLAB_LAB") or sys.exit("NETLAB_LAB is not set")
    lab = api("GET", f"/labs/{lab_id}")
    if a.cmd == "lab":
        show(lab, api("GET", "/templates"))
    elif a.cmd == "run":
        run(lab, api("GET", "/templates"), find(lab, a.node), a.lines, min(max(a.wait, 0.5), 60), a.shell)
    else:
        node = find(lab, a.node)
        api("POST", f"/labs/{lab_id}/nodes/{node['id']}/{a.cmd}")
        print(f"{node['name']}: {a.cmd} ok")


if __name__ == "__main__":
    main()
