"""NetLab web server: REST API, web UI and browser consoles (WebSocket)."""
import asyncio
import fcntl
import json
import os
import re
import shutil
import struct
import subprocess
import termios
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import ai
import auth as accounts
import engine
import updater

STATIC = Path(__file__).resolve().parent / "static"
FIRST_RUN = accounts.ensure_admin()

app = FastAPI(title="NetLab")


def auth(request: Request):
    header = request.headers.get("authorization", "")
    user = accounts.verify(header[7:]) if header.lower().startswith("bearer ") else None
    if not user:
        raise HTTPException(401, "sign in required")
    return user


@app.exception_handler(engine.LabError)
def lab_error(_, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.middleware("http")
async def revalidate_ui(request, call_next):
    # Browsers must recheck the UI files (ETag -> 304) so updates show up on a plain reload.
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
api = Depends(auth)


def lab_auth(lab_id: str, user: str = api):
    engine.can_access(lab_id, user)
    return user


lab_api = Depends(lab_auth)  # routes on one lab: only its owner (or admin) may use them


def admin_auth(user: str = api):
    if user != engine.ADMIN:  # restarts the server under every user's sessions
        raise HTTPException(403, "only admin can do this")
    return user


admin_api = Depends(admin_auth)


@app.post("/api/login")
def login(request: Request, body: dict = Body(...)):
    ip = request.client.host if request.client else "?"
    if accounts.throttled(ip):
        raise HTTPException(429, "Too many failed attempts. Try again in 5 minutes.")
    username, password = str(body.get("username", "")), str(body.get("password", ""))
    if not accounts.authenticate(username, password):
        accounts.record_fail(ip)
        raise HTTPException(401, "Wrong username or password.")
    accounts.clear_fails(ip)
    token, exp = accounts.issue(username)
    return {"token": token, "user": username, "exp": exp, "default_password": password == accounts.DEFAULT_PASSWORD}


@app.get("/api/me")
def me(user: str = api):
    return {"user": user}


@app.post("/api/password")
def change_password(body: dict = Body(...), user: str = api):
    if not accounts.authenticate(user, str(body.get("current", ""))):
        raise HTTPException(400, "Current password is wrong.")
    try:
        accounts.set_password(user, str(body.get("new", "")))
    except ValueError as e:
        raise HTTPException(400, str(e).capitalize() + ".")
    accounts.INITIAL_PW_FILE.unlink(missing_ok=True)
    token, exp = accounts.issue(user)  # the old token is revoked by the password change
    return {"token": token, "user": user, "exp": exp}


@app.get("/api/templates", dependencies=[api])
def templates():
    return engine.templates()


@app.get("/api/images", dependencies=[api])
def images():
    return engine.list_images()


shutil.rmtree(engine.UPLOADS, ignore_errors=True)  # parts left by an upload cut short by a restart
upload_lock = asyncio.Lock()


@app.put("/api/images/{name}", dependencies=[api])
async def upload_image(name: str, request: Request, filename: str, meta: str = "{}", disk_gb: int = 8):
    """Raw request body = the disk file (streamed to disk, so multi-GB images are fine)."""
    if upload_lock.locked():
        raise HTTPException(409, "Another image is being uploaded. Wait for it to finish.")
    async with upload_lock:
        try:
            meta = engine.clean_meta(json.loads(meta))
        except json.JSONDecodeError:
            raise HTTPException(400, "meta must be JSON")
        if not 1 <= disk_gb <= 512:
            raise HTTPException(400, "Disk size must be 1 to 512 GB.")
        size = request.headers.get("content-length")
        engine.check_new_image(name, filename, int(size) if size and size.isdigit() else None)
        engine.UPLOADS.mkdir(parents=True, exist_ok=True)
        tmp = engine.UPLOADS / f"{os.urandom(6).hex()}.part"
        try:
            written = 0
            with open(tmp, "wb") as f:
                async for chunk in request.stream():
                    written += len(chunk)
                    if written % (256 * 2**20) < len(chunk) and engine.disk_free() < engine.DISK_RESERVE:
                        raise engine.LabError("the server disk filled up during the upload")
                    await asyncio.to_thread(f.write, chunk)
            if not written:
                raise engine.LabError("the file is empty")
            return await asyncio.to_thread(engine.install_image, tmp, name, meta, filename, disk_gb)
        finally:
            tmp.unlink(missing_ok=True)


@app.delete("/api/images/{name}", dependencies=[api])
def delete_image(name: str):
    engine.delete_image(name)
    return {"ok": True}


# ---------------------------------------------------------------- updates (GitHub releases)

@app.get("/api/update", dependencies=[api])
async def update_info(force: int = 0):
    out = {"version": updater.version(), "status": updater.status()}
    try:
        out.update(await asyncio.to_thread(updater.check if force else updater.cached_check))
    except (updater.UpdateError, OSError, subprocess.SubprocessError) as e:
        out["error"] = str(e)
    return out


@app.get("/api/update/status")
def update_status():  # no sign-in: the page polls it while the server restarts, and it reveals nothing
    return {"version": updater.version(), **updater.status()}


@app.post("/api/update", dependencies=[admin_api])
def update_start(body: dict = Body(default={})):
    try:
        updater.start_background(body.get("version"))
    except updater.UpdateError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.put("/api/update/token", dependencies=[admin_api])
async def update_token(body: dict = Body(...)):
    token = str(body.get("token", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{20,255}", token):
        raise HTTPException(400, "that does not look like a GitHub token")
    updater.set_token(token)
    return await update_info(force=1)


@app.get("/api/ai")
def ai_providers(user: str = api):
    return {"providers": ai.providers(user)}


@app.put("/api/ai/9router")
def ai_router_connect(body: dict = Body(...), user: str = api):
    ai.set_router_config(user, body.get("url"), body.get("key"), body.get("model"))
    return {"providers": ai.providers(user)}


@app.delete("/api/ai/9router")
def ai_router_disconnect(user: str = api):
    ai.clear_router_config(user)
    return {"providers": ai.providers(user)}


@app.get("/api/labs")
def labs(user: str = api):
    return engine.list_labs(user)


@app.post("/api/labs")
def create_lab(body: dict = Body(...), user: str = api):
    if body.get("demo"):
        return engine.create_demo_lab(user)
    return engine.create_lab(body.get("name"), body.get("import"), user)


@app.get("/api/labs/{lab_id}", dependencies=[lab_api])
def get_lab(lab_id: str):
    lab = engine.load(lab_id)
    lab["status"] = engine.lab_status(lab_id)
    lab["link_status"] = engine.link_status(lab_id)
    lab["ai"] = AI_ACTIVITY.get(lab_id, {})
    return lab


@app.patch("/api/labs/{lab_id}", dependencies=[lab_api])
def update_lab(lab_id: str, body: dict = Body(...)):
    return engine.update_lab(lab_id, body)


@app.delete("/api/labs/{lab_id}", dependencies=[lab_api])
def delete_lab(lab_id: str):
    engine.delete_lab(lab_id)
    return {"ok": True}


# ------------------------------------------------ AI activity
# The assistant reaches one node at a time per lab (AI_LOCKS). Each access bumps the node's seq, so the
# browser opens that node's console (VMs) or its AI log tab (containers: commands run via exec, with log).
AI_LOCKS: dict = {}
AI_ACTIVITY: dict = {}  # lab id -> node id -> {"seq", "active", "log"?}
AI_SEQ = iter(range(1, 1 << 62))


def _ai_lock(lab_id):
    return AI_LOCKS.setdefault(lab_id, asyncio.Lock())


def _ai_mark(lab_id, node_id, active, log=None):
    rec = AI_ACTIVITY.setdefault(lab_id, {}).setdefault(node_id, {})
    if active:
        rec["seq"] = next(AI_SEQ)
    rec["active"] = active
    if log is not None:
        rec["log"] = log[-8192:]


@app.get("/api/labs/{lab_id}/status", dependencies=[lab_api])
def status(lab_id: str):
    return {"nodes": engine.lab_status(lab_id), "links": engine.link_status(lab_id),
            "ai": AI_ACTIVITY.get(lab_id, {})}


@app.post("/api/labs/{lab_id}/start", dependencies=[lab_api])
def start_lab(lab_id: str):
    return {"errors": engine.start_lab(lab_id)}


@app.post("/api/labs/{lab_id}/stop", dependencies=[lab_api])
def stop_lab(lab_id: str):
    engine.stop_lab(lab_id)
    return {"ok": True}


@app.post("/api/labs/{lab_id}/nodes", dependencies=[lab_api])
def add_node(lab_id: str, body: dict = Body(...)):
    return engine.add_node(lab_id, body)


@app.patch("/api/labs/{lab_id}/nodes/{node_id}", dependencies=[lab_api])
def update_node(lab_id: str, node_id: str, body: dict = Body(...)):
    return engine.update_node(lab_id, node_id, body)


@app.delete("/api/labs/{lab_id}/nodes/{node_id}", dependencies=[lab_api])
def delete_node(lab_id: str, node_id: str):
    engine.delete_node(lab_id, node_id)
    return {"ok": True}


@app.get("/api/labs/{lab_id}/nodes/{node_id}/config", dependencies=[lab_api])
def get_config(lab_id: str, node_id: str):
    return {"text": engine.get_config(lab_id, node_id)}


@app.put("/api/labs/{lab_id}/nodes/{node_id}/config", dependencies=[lab_api])
def set_config(lab_id: str, node_id: str, body: dict = Body(...)):
    engine.set_config(lab_id, node_id, str(body.get("text", "")))
    return {"ok": True}


@app.post("/api/labs/{lab_id}/nodes/{node_id}/exec", dependencies=[lab_api])
async def node_exec(lab_id: str, node_id: str, body: dict = Body(...)):
    lines = body.get("lines")
    if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
        raise HTTPException(400, "lines must be a list of strings")
    if not body.get("ai"):
        return await asyncio.to_thread(engine.exec_node, lab_id, node_id, lines, bool(body.get("shell")))
    async with _ai_lock(lab_id):
        _ai_mark(lab_id, node_id, True, "".join(f"\x1b[1m> {x}\x1b[0m\r\n" for x in lines))
        try:
            r = await asyncio.to_thread(engine.exec_node, lab_id, node_id, lines, bool(body.get("shell")))
        except engine.LabError as e:
            _ai_mark(lab_id, node_id, False, AI_ACTIVITY[lab_id][node_id]["log"] + f"*** {e} ***\r\n")
            raise
        _ai_mark(lab_id, node_id, False, AI_ACTIVITY[lab_id][node_id]["log"] + r["output"].replace("\n", "\r\n"))
        return r


@app.post("/api/labs/{lab_id}/nodes/{node_id}/{action}", dependencies=[lab_api])
def node_action(lab_id: str, node_id: str, action: str):
    fn = {"start": engine.start_node, "stop": engine.stop_node, "wipe": engine.wipe_node}.get(action)
    if not fn:
        raise HTTPException(404)
    fn(lab_id, node_id)
    return {"ok": True}


@app.post("/api/labs/{lab_id}/networks", dependencies=[lab_api])
def add_network(lab_id: str, body: dict = Body(...)):
    return engine.add_network(lab_id, body)


@app.patch("/api/labs/{lab_id}/networks/{net_id}", dependencies=[lab_api])
def update_network(lab_id: str, net_id: str, body: dict = Body(...)):
    return engine.update_network(lab_id, net_id, body)


@app.delete("/api/labs/{lab_id}/networks/{net_id}", dependencies=[lab_api])
def delete_network(lab_id: str, net_id: str):
    engine.delete_network(lab_id, net_id)
    return {"ok": True}


@app.post("/api/labs/{lab_id}/links", dependencies=[lab_api])
def add_link(lab_id: str, body: dict = Body(...)):
    return engine.add_link(lab_id, body)


@app.delete("/api/labs/{lab_id}/links/{link_id}", dependencies=[lab_api])
def delete_link(lab_id: str, link_id: str):
    engine.delete_link(lab_id, link_id)
    return {"ok": True}


# ---------------------------------------------------------------- consoles
# client -> server: JSON {"t":"i","d":"<keys>"} or {"t":"r","c":cols,"r":rows}
# server -> client: raw terminal bytes (binary frames)

@app.websocket("/ws/console/{lab_id}/{node_id}")
async def console(ws: WebSocket, lab_id: str, node_id: str, token: str = "", kind: str = "", history: int = 1,
                  ai: int = 0):
    await ws.accept()  # accept first so the browser sees close code 4401, not a bare handshake failure
    user = accounts.verify(token)
    if not user:
        await ws.close(code=4401)
        return
    try:
        await asyncio.to_thread(engine.can_access, lab_id, user)
        mode, target = await asyncio.to_thread(engine.console_target, lab_id, node_id, kind)
    except engine.LabError as e:
        await ws.send_bytes(f"\r\n*** {e} ***\r\n".encode())
        await ws.close()
        return
    if mode == "pty":
        await _pty_console(ws, target)
    elif ai:  # the assistant: wait for its turn in this lab, then tell it to start typing
        async with _ai_lock(lab_id):
            _ai_mark(lab_id, node_id, True)
            try:
                await ws.send_text("ready")
                await _tcp_console(ws, target, False)
            except (WebSocketDisconnect, RuntimeError):
                pass
            finally:
                _ai_mark(lab_id, node_id, False)
    else:
        await _tcp_console(ws, target, bool(history))


async def _pty_console(ws, argv, cwd=None, env=None):
    loop = asyncio.get_running_loop()
    master, slave = os.openpty()
    # setsid -c gives docker exec a controlling tty, so window resizes reach it
    proc = subprocess.Popen(["setsid", "-w", "-c"] + argv, stdin=slave, stdout=slave,
                            stderr=slave, close_fds=True, cwd=cwd, env=env)
    os.close(slave)
    queue: asyncio.Queue = asyncio.Queue()

    def readable():
        try:
            data = os.read(master, 65536)
        except OSError:
            data = b""
        queue.put_nowait(data)
        if not data:
            loop.remove_reader(master)

    loop.add_reader(master, readable)

    async def pump_out():
        while True:
            data = await queue.get()
            if not data:
                break
            await ws.send_bytes(data)
        await ws.close()

    out_task = asyncio.create_task(pump_out())
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("t") == "i":
                os.write(master, msg["d"].encode())
            elif msg.get("t") == "r":
                fcntl.ioctl(master, termios.TIOCSWINSZ,
                            struct.pack("HHHH", int(msg["r"]), int(msg["c"]), 0, 0))
    except (WebSocketDisconnect, RuntimeError, OSError, ValueError):
        pass
    finally:
        out_task.cancel()
        loop.remove_reader(master)
        os.close(master)
        proc.terminate()
        await asyncio.to_thread(proc.wait)


# AI assistant: mode "chat" runs the agent on the lab, "login"/"logout" manage the provider's sign-in
@app.websocket("/ws/ai/{provider}")
async def ai_console(ws: WebSocket, provider: str, token: str = "", lab: str = "", mode: str = "chat"):
    await ws.accept()
    user = accounts.verify(token)
    if not user:
        await ws.close(code=4401)
        return
    try:
        argv, cwd, env = await asyncio.to_thread(ai.session, provider, lab, mode, user)
    except engine.LabError as e:
        await ws.send_bytes(f"\r\n*** {e} ***\r\n".encode())
        await ws.close()
        return
    await _pty_console(ws, argv, cwd, env)


class SerialHub:
    """One connection to a VM's serial console (QEMU takes a single client), shared by every
    console tab and the AI assistant: input from any of them, output to all of them."""
    hubs: dict = {}
    tails: dict = {}  # port -> recent output, kept after the last client leaves
    TAIL = 4096  # recent output replayed to a new tab, so it shows the current prompt

    def __init__(self, port):
        self.port, self.clients, self.state = port, set(), {}
        self.tail = self.tails.pop(port, bytearray())
        self.reader = self.writer = self.task = None

    @classmethod
    async def get(cls, port):
        hub = cls.hubs.get(port)
        if hub is None:
            hub = cls(port)
            hub.reader, hub.writer = await asyncio.open_connection("127.0.0.1", port)  # OSError if the VM is gone
            if port in cls.hubs:  # another client connected meanwhile
                hub.writer.close()
                return cls.hubs[port]
            cls.hubs[port] = hub
            hub.task = asyncio.create_task(hub._pump())
        return hub

    async def _pump(self):
        try:
            while data := await self.reader.read(65536):
                data = engine.strip_telnet(data, self.state)
                self.tail = (self.tail + data)[-self.TAIL:]
                for ws in list(self.clients):
                    try:
                        await ws.send_bytes(data)
                    except Exception:
                        self.clients.discard(ws)
        except OSError:
            pass
        self._close()
        for ws in list(self.clients):
            try:
                await ws.close()
            except Exception:
                pass

    def _close(self):
        if self.hubs.get(self.port) is self:
            del self.hubs[self.port]
            if len(self.tails) > 256:
                self.tails.clear()
            self.tails[self.port] = self.tail
        self.writer.close()

    async def join(self, ws, history):
        if history and self.tail:
            await ws.send_bytes(bytes(self.tail))
        self.clients.add(ws)

    def leave(self, ws):
        self.clients.discard(ws)
        if not self.clients:
            self.task.cancel()
            self._close()

    async def write(self, data: bytes):
        self.writer.write(data.replace(b"\xff", b"\xff\xff"))
        await self.writer.drain()


async def _tcp_console(ws, port, history=True):
    try:
        hub = await SerialHub.get(port)
    except OSError as e:
        await ws.send_bytes(f"\r\n*** serial console unavailable: {e} ***\r\n".encode())
        await ws.close()
        return
    await hub.join(ws, history)
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("t") == "i":
                await hub.write(msg["d"].encode())
    except (WebSocketDisconnect, RuntimeError, OSError, ValueError):
        pass
    finally:
        hub.leave(ws)


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("NETLAB_HOST", "0.0.0.0")
    port = int(os.environ.get("NETLAB_PORT", "8765"))
    print(f"NetLab on http://{host}:{port}/", flush=True)
    if FIRST_RUN:
        print(f"Created user 'admin' with password '{accounts.DEFAULT_PASSWORD}'; change it after signing in", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")
