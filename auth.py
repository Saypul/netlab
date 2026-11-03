"""NetLab accounts and JWT sessions.

Passwords: PBKDF2-SHA256. Sessions: HS256 JWT carrying a per-user "pv"
(password version) claim, so changing a password revokes every older token.

CLI (run as root from /root/netlab):
    .venv/bin/python auth.py list
    .venv/bin/python auth.py passwd <user>     # create user or reset password
    .venv/bin/python auth.py deluser <user>
"""
import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time

import jwt

import engine

USERS = engine.DATA / "users.json"
SECRET_FILE = engine.DATA / "jwt_secret"
INITIAL_PW_FILE = engine.DATA / "initial_password"
DEFAULT_PASSWORD = "netlab"
TTL = int(os.environ.get("NETLAB_JWT_TTL", 12 * 3600))
ITERATIONS = 600_000
MAX_FAILS, FAIL_WINDOW = 5, 300

_lock = threading.Lock()


def _private_write(path, text):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.chmod(0o600)
    tmp.replace(path)


if not SECRET_FILE.exists():
    _private_write(SECRET_FILE, secrets.token_urlsafe(48))
SECRET = SECRET_FILE.read_text().strip()


def _load():
    return json.loads(USERS.read_text()) if USERS.exists() else {}


def _hash(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def _verify_hash(password, stored):
    _, iterations, salt, dk = stored.split("$")
    got = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
    return hmac.compare_digest(got, base64.b64decode(dk))


_DUMMY_HASH = _hash(secrets.token_hex(8))  # unknown usernames take as long as wrong passwords


def list_users():
    return sorted(_load())


def set_password(username, password):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", username):
        raise ValueError("username: 1-32 letters, digits, _ . -")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    _store(username, password)


def _store(username, password):
    with _lock:
        users = _load()
        users[username] = {"hash": _hash(password), "pv": secrets.token_hex(4)}
        _private_write(USERS, json.dumps(users, indent=2))


def delete_user(username):
    with _lock:
        users = _load()
        if users.pop(username, None) is None:
            raise ValueError("no such user")
        _private_write(USERS, json.dumps(users, indent=2))


def ensure_admin():
    """First run: create 'admin' with the default password (shorter than the minimum, like EVE-NG's);
    data/initial_password stays until it is changed."""
    if _load():
        return False
    _store("admin", DEFAULT_PASSWORD)
    _private_write(INITIAL_PW_FILE, f"username: admin\npassword: {DEFAULT_PASSWORD}\n")
    return True


def authenticate(username, password):
    rec = _load().get(username)
    ok = _verify_hash(password, rec["hash"] if rec else _DUMMY_HASH)
    return bool(rec) and ok


def issue(username):
    now = int(time.time())
    pv = _load()[username]["pv"]
    token = jwt.encode({"sub": username, "pv": pv, "iat": now, "exp": now + TTL}, SECRET, algorithm="HS256")
    return token, now + TTL


def verify(token):
    """Return the username for a valid, unrevoked token, else None."""
    try:
        claims = jwt.decode(token, SECRET, algorithms=["HS256"], options={"require": ["exp", "sub", "pv"]})
    except jwt.PyJWTError:
        return None
    rec = _load().get(claims["sub"])
    if not rec or not hmac.compare_digest(rec["pv"], str(claims["pv"])):
        return None
    return claims["sub"]


# ---------------------------------------------------------------- login throttling (per client IP)

_fails: dict = {}


def throttled(ip):
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
    _fails[ip] = recent
    return len(recent) >= MAX_FAILS


def record_fail(ip):
    _fails.setdefault(ip, []).append(time.time())


def clear_fails(ip):
    _fails.pop(ip, None)


if __name__ == "__main__":
    cmd, *args = sys.argv[1:] or ["help"]
    try:
        if cmd == "list":
            print("\n".join(list_users()) or "(no users)")
        elif cmd == "passwd" and len(args) == 1:
            pw = getpass.getpass(f"New password for {args[0]}: ")
            if pw != getpass.getpass("Repeat: "):
                sys.exit("passwords do not match")
            set_password(args[0], pw)
            print(f"password set for {args[0]}; their existing sessions are signed out")
        elif cmd == "deluser" and len(args) == 1:
            delete_user(args[0])
            print(f"deleted {args[0]}")
        else:
            print(__doc__)
    except ValueError as e:
        sys.exit(str(e))
