"""Online updates from GitHub: releases are git tags vX.Y.Z on the NetLab repository.

check()  fetches the tags and reports whether a newer release exists (with its notes and commits).
apply()  checks that release out, installs its Python requirements and restarts the service; if the
         new version does not answer within a minute it goes back to the previous one.
data/ (labs, images, users, AI sign-ins) is untracked, so updates never touch it.
A private repository needs a read-only GitHub token in data/github_token.
"""
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("NETLAB_DATA", BASE / "data"))
TOKEN_FILE = DATA / "github_token"
STATUS_FILE = DATA / "update.json"
CHECK_FILE = DATA / "update-check.json"
SERVICE = os.environ.get("NETLAB_SERVICE", "netlab")
PORT = os.environ.get("NETLAB_PORT", "8765")
TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


class UpdateError(Exception):
    pass


def version():
    try:
        return (BASE / "VERSION").read_text().strip()
    except OSError:
        return "0.0.0"


def _key(v):
    m = TAG.fullmatch("v" + v.lstrip("v"))
    return tuple(map(int, m.groups())) if m else None


def _token():
    try:
        return TOKEN_FILE.read_text().strip()
    except OSError:
        return os.environ.get("NETLAB_GITHUB_TOKEN", "")


def set_token(token):
    DATA.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.touch(mode=0o600)
    TOKEN_FILE.chmod(0o600)
    TOKEN_FILE.write_text(token.strip() + "\n")


def _git(*args, timeout=120):
    cmd = ["git", "-C", str(BASE)]
    if token := _token():  # never stored in .git/config; passed per call
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        cmd += ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    r = subprocess.run(cmd + list(args), capture_output=True, text=True, timeout=timeout, env=env)
    if r.returncode != 0:
        err = r.stderr.strip().splitlines()[-1:] or ["git failed"]
        if "Authentication failed" in r.stderr or "could not read Username" in r.stderr or "not found" in r.stderr:
            err = ["GitHub refused access: the repository is private, set a token with `netlab token`"]
        raise UpdateError(err[0])
    return r.stdout


def check():
    """{current, latest, available, notes, changes} for the newest release tag on GitHub."""
    if not (BASE / ".git").exists():
        raise UpdateError(f"{BASE} is not a git checkout; reinstall with install.sh to get updates")
    _git("fetch", "--quiet", "--tags", "--force", "origin")
    tags = [t for t in _git("tag", "--list", "v*").split() if _key(t)]
    cur = version()
    best = max(tags, key=_key, default=None)
    out = {"current": cur, "latest": best.lstrip("v") if best else cur, "available": False,
           "notes": "", "changes": [], "checked": int(time.time())}
    if best and _key(best) > _key(cur):
        out["available"] = True
        out["notes"] = _git("tag", "--list", best, "--format=%(contents)").strip()
        base = f"v{cur}" if f"v{cur}" in tags else "HEAD"
        out["changes"] = _git("log", "--format=%s", "--no-merges", f"{base}..{best}").splitlines()[:50]
    CHECK_FILE.write_text(json.dumps(out))
    return out


def cached_check(max_age=3600):
    try:
        c = json.loads(CHECK_FILE.read_text())
        if time.time() - c["checked"] < max_age and c["current"] == version():
            return c
    except (OSError, ValueError, KeyError):
        pass
    return check()


def status():
    try:
        return json.loads(STATUS_FILE.read_text())
    except (OSError, ValueError):
        return {"state": "idle"}


def _status(**kw):
    STATUS_FILE.write_text(json.dumps(dict(kw, time=int(time.time()))))


def _healthy(timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=3) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(2)
    return False


def _install(ref):
    _git("checkout", "--quiet", "--detach", ref)
    subprocess.run([str(BASE / ".venv/bin/pip"), "install", "-q", "-r", str(BASE / "requirements.txt")],
                   check=True, capture_output=True, timeout=600)
    subprocess.run(["systemctl", "restart", SERVICE], check=True, capture_output=True, timeout=120)


def apply(target=None, spawned=False):
    """Update to target (default: the newest release). Runs outside the web server process."""
    if not spawned and status().get("state") == "running" and time.time() - status().get("time", 0) < 900:
        raise UpdateError("an update is already running")
    if _git("status", "--porcelain", "--untracked-files=no").strip():
        raise UpdateError(f"{BASE} has local code changes; commit or discard them before updating")
    info = check()
    target = (target or info["latest"]).lstrip("v")
    if not _key(target) or _key(target) <= _key(version()):
        raise UpdateError(f"NetLab {version()} is up to date")
    prev, prev_version = _git("rev-parse", "HEAD").strip(), version()
    _status(state="running", step=f"installing {target}", target=target, previous=prev_version)
    try:
        _install(f"v{target}")
        _status(state="running", step="waiting for the new version to start", target=target, previous=prev_version)
        if not _healthy():
            raise UpdateError(f"NetLab {target} did not start")
    except (UpdateError, subprocess.SubprocessError, OSError) as e:
        _status(state="running", step=f"rolling back to {prev_version}", target=target, previous=prev_version)
        try:
            _install(prev)
        except (UpdateError, subprocess.SubprocessError, OSError) as e2:
            _status(state="failed", error=f"{e}; rollback failed too: {e2}", target=target, previous=prev_version)
            raise UpdateError(f"{e}; rollback failed too: {e2}")
        _status(state="failed", error=f"{e}; back on {prev_version}", target=target, previous=prev_version)
        raise UpdateError(f"{e}; back on {prev_version}")
    _status(state="done", target=target, previous=prev_version)
    return target


def start_background(target=None):
    """Run apply() in its own process: restarting the service must not stop the update."""
    if status().get("state") == "running" and time.time() - status().get("time", 0) < 900:
        raise UpdateError("an update is already running")
    _status(state="running", step="starting", target=target or "")
    log = open(DATA / "update.log", "ab")
    subprocess.Popen([sys.executable, str(BASE / "updater.py"), "apply-spawned"] + ([target] if target else []),
                     cwd=BASE, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)


if __name__ == "__main__":
    try:
        if sys.argv[1:2] in (["apply"], ["apply-spawned"]):
            print("updated to", apply(sys.argv[2] if len(sys.argv) > 2 else None, sys.argv[1] == "apply-spawned"))
        else:
            print(json.dumps(check(), indent=1))
    except UpdateError as e:
        if status().get("state") == "running":
            _status(state="failed", error=str(e))
        sys.exit(f"update: {e}")
