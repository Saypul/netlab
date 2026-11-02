"""netlab: manage a NetLab server from the shell (installed as /usr/local/bin/netlab by install.sh)."""
import argparse
import getpass
import os
import subprocess
import sys
from pathlib import Path

import updater

BASE = Path(__file__).resolve().parent


def cmd_update(a):
    try:
        info = updater.check()
    except updater.UpdateError as e:
        sys.exit(f"update: {e}")
    if not info["available"]:
        print(f"NetLab {info['current']} is up to date.")
        return
    print(f"NetLab {info['latest']} is available (installed: {info['current']}).")
    if info["notes"]:
        print("\n" + info["notes"])
    for line in info["changes"]:
        print("  - " + line)
    if a.check:
        return
    if not a.yes and input(f"\nUpdate to {info['latest']} now? Running labs keep running. [y/N] ").lower() != "y":
        return
    try:
        print("Updated to", updater.apply(info["latest"]))
    except updater.UpdateError as e:
        sys.exit(f"update: {e}")


def cmd_token(a):
    token = a.token or getpass.getpass("GitHub token (read-only access to the NetLab repository): ")
    updater.set_token(token)
    try:
        updater.check()
        print("Token saved; GitHub access works.")
    except updater.UpdateError as e:
        sys.exit(f"Token saved, but checking GitHub failed: {e}")


def main():
    p = argparse.ArgumentParser(prog="netlab", description="Manage this NetLab server.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version", help="show the installed version")
    u = sub.add_parser("update", help="update to the newest release on GitHub")
    u.add_argument("--check", action="store_true", help="only show whether an update exists")
    u.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    t = sub.add_parser("token", help="set the GitHub token used for updates (private repository)")
    t.add_argument("token", nargs="?")
    for c in ("status", "restart", "logs"):
        sub.add_parser(c, help=f"service {c}")
    us = sub.add_parser("user", help="manage users: list | passwd <user> | deluser <user>")
    us.add_argument("args", nargs=argparse.REMAINDER)
    a = p.parse_args()
    if a.cmd == "version":
        print(updater.version())
    elif a.cmd == "update":
        cmd_update(a)
    elif a.cmd == "token":
        cmd_token(a)
    elif a.cmd == "user":
        os.execv(sys.executable, [sys.executable, str(BASE / "auth.py")] + (a.args or ["list"]))
    elif a.cmd == "logs":
        os.execvp("journalctl", ["journalctl", "-u", updater.SERVICE, "-f", "-n", "100"])
    else:
        sys.exit(subprocess.run(["systemctl", a.cmd, updater.SERVICE]).returncode)


if __name__ == "__main__":
    main()
