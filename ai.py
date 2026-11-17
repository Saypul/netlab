"""AI assistant plugin: Claude Code, Codex or OpenCode in a browser terminal, working on a lab.

Each NetLab user keeps their own sign-in per provider under data/ai/users/<user>/<provider> (its HOME),
so users never share an AI account or session history, and NetLab never borrows a login that already
exists on this server. "9router" is Claude Code talking to a 9Router proxy (github.com/decolua/9router)
with the user's own 9Router API key and model, kept in data/ai/users/<user>/9router.json.
A session runs in data/ai/work/<lab id>/, whose AGENTS.md / CLAUDE.md describe the lab and the
`nlctl` helper the agent uses to reach nodes.
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import engine

BASE = Path(__file__).resolve().parent
ROOT = engine.DATA / "ai"
MIN_RAM_MB = int(os.environ.get("NETLAB_AI_MIN_RAM_MB", "400"))
NINEROUTER_URL = os.environ.get("NETLAB_9ROUTER_URL", "http://127.0.0.1:20128")

PROVIDERS = {
    "claude": {"label": "Claude Code", "bin": "claude", "account": "Claude account (Pro/Max) or Anthropic API key",
               "login": ["auth", "login"], "logout": ["auth", "logout"], "status": ["auth", "status"],
               "install": "npm install -g @anthropic-ai/claude-code"},
    "codex": {"label": "Codex", "bin": "codex", "account": "ChatGPT account (Plus/Pro) or OpenAI API key",
              "login": ["login", "--device-auth"], "logout": ["logout"], "status": ["login", "status"],
              "install": "npm install -g @openai/codex",
              # Codex runs commands in its sandbox; nlctl talks to the NetLab server on 127.0.0.1, so allow network.
              # No shared daemon: it would keep the first session's lab and settings for later sessions
              "chat": ["--no-daemon", "-c", "sandbox_workspace_write.network_access=true",
                       "-c", "check_for_update_on_startup=false"]},
    "opencode": {"label": "OpenCode", "bin": "opencode", "account": "open source; free models, or sign in to "
                 "Claude, ChatGPT, GitHub Copilot and other providers",
                 "login": ["auth", "login"], "logout": ["auth", "logout"], "status": None,
                 "install": "npm install -g opencode-ai"},
    # Claude Code with ANTHROPIC_BASE_URL pointing at 9Router; sign-in is the API key from the 9Router dashboard
    "9router": {"label": "9Router (Claude Code)", "bin": "claude", "account": "9Router API key and model: routes "
                "to the providers and combos connected in the 9Router dashboard",
                "login": None, "logout": None, "status": None,
                "install": "npm install -g @anthropic-ai/claude-code 9router"},
}

CODEX_RULES = """# written by NetLab
prefix_rule(pattern=["nlctl", "lab"], decision="allow")
prefix_rule(pattern=["nlctl", ["run", "start", "stop"]], decision="prompt")
"""


def _bin(key):
    return shutil.which(PROVIDERS[key]["bin"]) or shutil.which(PROVIDERS[key]["bin"], path="/root/.local/bin")


def _user_dir(user):
    if not user or user.startswith(".") or "/" in user:
        raise engine.LabError("invalid user")
    return ROOT / "users" / user


def _home(key, user):
    h = _user_dir(user) / key
    legacy = ROOT / "home" / key  # sign-ins from before per-user homes stay with admin
    if not h.exists() and user == engine.ADMIN and legacy.is_dir():
        h.parent.mkdir(parents=True, exist_ok=True)
        legacy.rename(h)
    if not h.exists():
        h.mkdir(parents=True)
        if key == "claude":  # NetLab terminals are light; skip the theme question on first start
            (h / ".claude.json").write_text('{"theme": "light"}\n')
        elif key == "9router":  # signed in through the API key: skip the account login screen too
            (h / ".claude.json").write_text('{"theme": "light", "hasCompletedOnboarding": true}\n')
    return h


def router_config(user):
    try:
        return json.loads((_user_dir(user) / "9router.json").read_text())
    except (OSError, ValueError):
        return None


def set_router_config(user, url, key, model):
    url = (url or NINEROUTER_URL).strip().rstrip("/")
    if url.endswith("/v1"):  # Claude Code adds /v1/messages itself
        url = url[:-3]
    if not url.startswith(("http://", "https://")):
        raise engine.LabError("9Router URL must start with http:// or https://")
    key, model = (key or "").strip(), (model or "").strip()
    if not key or not model:
        raise engine.LabError("enter the 9Router API key and a model (e.g. cc/claude-sonnet-4-5 or a combo name)")
    try:  # catch a wrong URL or key now rather than inside the agent; an empty request costs no tokens
        req = urllib.request.Request(url + "/v1/messages", data=b"{}", headers={
            "authorization": "Bearer " + key, "content-type": "application/json"})
        urllib.request.urlopen(req, timeout=8).close()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise engine.LabError("9Router rejected the API key")
    except OSError as e:
        raise engine.LabError(f"cannot reach 9Router at {url}: {getattr(e, 'reason', e)}")
    d = _user_dir(user)
    d.mkdir(parents=True, exist_ok=True)
    f = d / "9router.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"url": url, "key": key, "model": model}))
    tmp.chmod(0o600)
    tmp.replace(f)


def clear_router_config(user):
    (_user_dir(user) / "9router.json").unlink(missing_ok=True)


def _env(key, user, lab_id=None):
    path = os.pathsep.join([str(ROOT / "bin"), str(Path(sys.executable).parent), os.environ.get("PATH", "")])
    env = {"HOME": str(_home(key, user)), "PATH": path, "TERM": "xterm-256color", "COLORTERM": "truecolor",
           "LANG": os.environ.get("LANG", "C.UTF-8"), "DISABLE_AUTOUPDATER": "1"}
    if key == "9router" and (cfg := router_config(user)):
        env.update(ANTHROPIC_BASE_URL=cfg["url"], ANTHROPIC_AUTH_TOKEN=cfg["key"], ANTHROPIC_MODEL=cfg["model"],
                   ANTHROPIC_DEFAULT_OPUS_MODEL=cfg["model"], ANTHROPIC_DEFAULT_SONNET_MODEL=cfg["model"],
                   ANTHROPIC_DEFAULT_HAIKU_MODEL=cfg["model"], CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    if lab_id:  # nlctl reaches VM consoles through the NetLab server, as this user
        env.update(NETLAB_LAB=lab_id, NETLAB_USER=user, NETLAB_PORT=os.environ.get("NETLAB_PORT", "8765"))
    return env


def _signed_in(key, user):
    p = PROVIDERS[key]
    if key == "9router":
        return router_config(user) is not None
    if p["status"] is None:  # opencode: credentials live in a JSON file
        try:
            return bool(json.loads((_home(key, user) / ".local/share/opencode/auth.json").read_text()))
        except (OSError, ValueError):
            return False
    try:
        r = subprocess.run([_bin(key)] + p["status"], env=_env(key, user), capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def providers(user):
    keys = list(PROVIDERS)
    installed = {k: bool(_bin(k)) for k in keys}
    with ThreadPoolExecutor(len(keys)) as pool:
        signed = dict(zip(keys, pool.map(lambda k: installed[k] and _signed_in(k, user), keys)))
    out = [{"key": k, "label": PROVIDERS[k]["label"], "account": PROVIDERS[k]["account"],
            "installed": installed[k], "signed_in": signed[k], "install": PROVIDERS[k]["install"]} for k in keys]
    cfg = router_config(user) or {}
    out[keys.index("9router")].update(url=cfg.get("url") or NINEROUTER_URL, model=cfg.get("model", ""))
    return out


def _write_context(lab):
    work = ROOT / "work" / lab["id"]
    work.mkdir(parents=True, exist_ok=True)
    (work / "AGENTS.md").write_text(f"""# NetLab lab "{lab['name']}"

You help the user build and troubleshoot this network lab. Nodes are FRRouting/Alpine containers and
QEMU VMs (MikroTik, Huawei, VyOS, ...). Reach them only through `nlctl`; it already knows this lab.

    nlctl lab                         # nodes, state, interfaces, cables (always check this first)
    nlctl run R1 "show ip route"      # run commands on a node; one quoted argument per line
    nlctl run R1 --wait 8 "cmd"       # wait longer for slow output (seconds, default 4)
    nlctl start R1  /  nlctl stop R1

- FRR routers take vtysh commands, e.g. `nlctl run R1 "conf t" "router ospf" "network 10.0.0.0/8 area 0"`,
  then `nlctl run R1 "write memory"` to keep the config. Add `--shell` to run a Linux shell command instead.
- PCs (Alpine) take shell commands: `nlctl run PC1 "ip addr"`.
- QEMU VMs are driven through their serial console: the text is typed exactly as given, so use the
  vendor's CLI (RouterOS, VRP, VyOS, ...). A VM that has just started may still be booting or at a
  login prompt; check the output and log in first if needed.
- VM consoles are shared with the user's console tabs: they see what you type, and the console may be in
  the middle of something the user started (a login prompt, a config mode, a pager). Read the output and
  adapt; send an empty line ("") to see the current prompt.
- To find config mistakes, compare both ends of each cable (`nlctl lab`): addresses and masks, interface
  state, routing protocol settings. Explain what is wrong and the fix before changing anything.
- Work on one node at a time and run nlctl commands one after another, never in parallel: NetLab opens
  each node's console for the user as you reach it, so they can follow along.
- Interface names in `nlctl lab` are the names the device uses. Confirm before stopping nodes or wiping config.
""")
    (work / "CLAUDE.md").write_text("@AGENTS.md\n")
    return work


def _write_nlctl():
    bindir = ROOT / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    script = bindir / "nlctl"
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{BASE / "nlctl.py"}" "$@"\n')
    script.chmod(0o755)


def session(key, lab_id, mode, user):
    """argv, cwd and env for a terminal session: mode "chat" opens the agent, "login"/"logout" manage sign-in."""
    if key not in PROVIDERS:
        raise engine.LabError("unknown AI provider")
    exe = _bin(key) or engine._raise(f"{PROVIDERS[key]['label']} is not installed: {PROVIDERS[key]['install']}")
    if mode in ("login", "logout"):
        if not PROVIDERS[key][mode]:
            raise engine.LabError(f"{PROVIDERS[key]['label']} signs in with Connect, not in a terminal")
        return [exe] + PROVIDERS[key][mode], str(_home(key, user)), _env(key, user)
    if mode != "chat":
        raise engine.LabError("invalid mode")
    lab = engine.can_access(lab_id, user)
    if key == "9router" and not router_config(user):
        raise engine.LabError("connect 9Router first: enter its API key and model")
    avail = engine._mem_available_mb()
    if avail < MIN_RAM_MB:
        raise engine.LabError(f"not enough free memory for the AI assistant: needs {MIN_RAM_MB} MB, "
                              f"server has {avail} MB available")
    _write_nlctl()
    if key == "codex":  # ask before nlctl touches a node; reading the topology needs no approval
        rules = _home(key, user) / ".codex" / "rules"
        rules.mkdir(parents=True, exist_ok=True)
        (rules / "netlab.rules").write_text(CODEX_RULES)
    return [exe] + PROVIDERS[key].get("chat", []), str(_write_context(lab)), _env(key, user, lab["id"])
