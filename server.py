"""NetLab web server: REST API, web UI and browser consoles (WebSocket)."""
import asyncio
import fcntl
import json
import os
import shutil
import struct
import subprocess
import termios
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import auth as accounts
import engine

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
    return {"token": token, "user": username, "exp": exp}


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


@app.get("/api/labs", dependencies=[api])
def labs():
    return engine.list_labs()


@app.post("/api/labs", dependencies=[api])
def create_lab(body: dict = Body(...)):
    if body.get("demo"):
        return engine.create_demo_lab()
    return engine.create_lab(body.get("name"), body.get("import"))


@app.get("/api/labs/{lab_id}", dependencies=[api])
def get_lab(lab_id: str):
    lab = engine.load(lab_id)
    lab["status"] = engine.lab_status(lab_id)
    lab["link_status"] = engine.link_status(lab_id)
    return lab


@app.patch("/api/labs/{lab_id}", dependencies=[api])
def update_lab(lab_id: str, body: dict = Body(...)):
    return engine.update_lab(lab_id, body)


@app.delete("/api/labs/{lab_id}", dependencies=[api])
def delete_lab(lab_id: str):
    engine.delete_lab(lab_id)
    return {"ok": True}


@app.get("/api/labs/{lab_id}/status", dependencies=[api])
def status(lab_id: str):
    return {"nodes": engine.lab_status(lab_id), "links": engine.link_status(lab_id)}


@app.post("/api/labs/{lab_id}/start", dependencies=[api])
def start_lab(lab_id: str):
    return {"errors": engine.start_lab(lab_id)}


@app.post("/api/labs/{lab_id}/stop", dependencies=[api])
def stop_lab(lab_id: str):
    engine.stop_lab(lab_id)
    return {"ok": True}


@app.post("/api/labs/{lab_id}/nodes", dependencies=[api])
def add_node(lab_id: str, body: dict = Body(...)):
    return engine.add_node(lab_id, body)


@app.patch("/api/labs/{lab_id}/nodes/{node_id}", dependencies=[api])
def update_node(lab_id: str, node_id: str, body: dict = Body(...)):
    return engine.update_node(lab_id, node_id, body)


@app.delete("/api/labs/{lab_id}/nodes/{node_id}", dependencies=[api])
def delete_node(lab_id: str, node_id: str):
    engine.delete_node(lab_id, node_id)
    return {"ok": True}


@app.get("/api/labs/{lab_id}/nodes/{node_id}/config", dependencies=[api])
def get_config(lab_id: str, node_id: str):
    return {"text": engine.get_config(lab_id, node_id)}


@app.put("/api/labs/{lab_id}/nodes/{node_id}/config", dependencies=[api])
def set_config(lab_id: str, node_id: str, body: dict = Body(...)):
    engine.set_config(lab_id, node_id, str(body.get("text", "")))
    return {"ok": True}


@app.post("/api/labs/{lab_id}/nodes/{node_id}/{action}", dependencies=[api])
def node_action(lab_id: str, node_id: str, action: str):
    fn = {"start": engine.start_node, "stop": engine.stop_node, "wipe": engine.wipe_node}.get(action)
    if not fn:
        raise HTTPException(404)
    fn(lab_id, node_id)
    return {"ok": True}


@app.post("/api/labs/{lab_id}/networks", dependencies=[api])
def add_network(lab_id: str, body: dict = Body(...)):
    return engine.add_network(lab_id, body)


@app.patch("/api/labs/{lab_id}/networks/{net_id}", dependencies=[api])
def update_network(lab_id: str, net_id: str, body: dict = Body(...)):
    return engine.update_network(lab_id, net_id, body)


@app.delete("/api/labs/{lab_id}/networks/{net_id}", dependencies=[api])
def delete_network(lab_id: str, net_id: str):
    engine.delete_network(lab_id, net_id)
    return {"ok": True}


@app.post("/api/labs/{lab_id}/links", dependencies=[api])
def add_link(lab_id: str, body: dict = Body(...)):
    return engine.add_link(lab_id, body)


@app.delete("/api/labs/{lab_id}/links/{link_id}", dependencies=[api])
def delete_link(lab_id: str, link_id: str):
    engine.delete_link(lab_id, link_id)
    return {"ok": True}


# ---------------------------------------------------------------- consoles
# client -> server: JSON {"t":"i","d":"<keys>"} or {"t":"r","c":cols,"r":rows}
# server -> client: raw terminal bytes (binary frames)

def _strip_telnet(buf: bytes, state: dict) -> bytes:
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


@app.websocket("/ws/console/{lab_id}/{node_id}")
async def console(ws: WebSocket, lab_id: str, node_id: str, token: str = "", kind: str = ""):
    await ws.accept()  # accept first so the browser sees close code 4401, not a bare handshake failure
    if not accounts.verify(token):
        await ws.close(code=4401)
        return
    try:
        mode, target = await asyncio.to_thread(engine.console_target, lab_id, node_id, kind)
    except engine.LabError as e:
        await ws.send_bytes(f"\r\n*** {e} ***\r\n".encode())
        await ws.close()
        return
    if mode == "pty":
        await _pty_console(ws, target)
    else:
        await _tcp_console(ws, target)


async def _pty_console(ws, argv):
    loop = asyncio.get_running_loop()
    master, slave = os.openpty()
    # setsid -c gives docker exec a controlling tty, so window resizes reach it
    proc = subprocess.Popen(["setsid", "-w", "-c"] + argv, stdin=slave, stdout=slave,
                            stderr=slave, close_fds=True)
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


async def _tcp_console(ws, port):
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError as e:
        await ws.send_bytes(f"\r\n*** serial console unavailable: {e} ***\r\n".encode())
        await ws.close()
        return
    state: dict = {}

    async def pump_out():
        while data := await reader.read(65536):
            await ws.send_bytes(_strip_telnet(data, state))
        await ws.close()

    out_task = asyncio.create_task(pump_out())
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("t") == "i":
                writer.write(msg["d"].encode().replace(b"\xff", b"\xff\xff"))
                await writer.drain()
    except (WebSocketDisconnect, RuntimeError, OSError, ValueError):
        pass
    finally:
        out_task.cancel()
        writer.close()


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("NETLAB_HOST", "0.0.0.0")
    port = int(os.environ.get("NETLAB_PORT", "8765"))
    print(f"NetLab on http://{host}:{port}/", flush=True)
    if FIRST_RUN:
        print(f"Created user 'admin'; its password is in {accounts.INITIAL_PW_FILE}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")
