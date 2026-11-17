"use strict";
const $ = (s) => document.querySelector(s);
const SVGNS = "http://www.w3.org/2000/svg";
// Device groups in the palette. QEMU images join one via "category" in their netlab.json.
const GROUPS = [["router", "Router"], ["switch", "Switch"], ["firewall", "Firewall"], ["host", "PC / Host"], ["cloud", "Cloud"], ["other", "Other"]];
const CATEGORY_ICON = { router: "ico-router", switch: "ico-switch", firewall: "ico-firewall", host: "ico-pc", cloud: "ico-cloud", other: "ico-server" };
const DRAG_TYPE = "application/x-netlab-device";

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
  del(k) { try { localStorage.removeItem(k); } catch {} },
};

const state = {
  token: store.get("netlab.jwt") || "",   // JWT from /api/login
  user: null,
  tpl: null,          // /api/templates response
  labs: [],
  lab: null,
  status: {},
  linkStatus: {},     // link id -> "up" | "down", from the server
  linkBornAt: new Map(), linkUpAt: new Map(),  // when a link appeared / came up, for one-shot animations
  sel: null,          // {kind:'node'|'network'|'link', id}
  mode: null,         // {place:{template,image?}|{network:true}} or {connect:true, from:null|end}
  consoles: [],
  aiActive: new Set(),  // node ids the AI assistant is working on right now
  active: null,
};

// ---------------------------------------------------------------- api

async function api(method, path, body) {
  const r = await fetch("/api" + path, {
    method, headers: { authorization: "Bearer " + state.token, "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (r.status === 401) { await login(state.token ? "Your session has expired. Sign in again." : ""); return api(method, path, body); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { toast(j.detail || r.statusText); throw new Error(j.detail || r.statusText); }
  return j;
}

let toastTimer;
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), 4000);
}

// ---------------------------------------------------------------- modal

function modal({ title, body, ok = "OK", wide = false, focus }) {
  return new Promise((resolve) => {
    $("#modalTitle").textContent = title;
    $("#modalBody").innerHTML = "";
    $("#modalBody").append(body);
    $("#modalOk").textContent = ok;
    $("#modalForm").classList.toggle("wide", wide);
    $("#modal").hidden = false;
    (focus || body.querySelector("input,textarea"))?.focus();
    const done = (v) => { $("#modal").hidden = true; $("#modalForm").onsubmit = null; $("#modalCancel").onclick = null; resolve(v); };
    $("#modalForm").onsubmit = (e) => { e.preventDefault(); done(true); };
    $("#modalCancel").onclick = () => done(false);
  });
}

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "text") e.textContent = v;
    else if (v !== false && v != null) e.setAttribute(k, v === true ? "" : v);
  }
  e.append(...kids);
  return e;
}

async function ask(title, value = "", label = "Name") {
  const input = el("input", { type: "text", value });
  const ok = await modal({ title, body: el("div", {}, el("p", { text: label }), input) });
  return ok ? input.value.trim() : null;
}

// ---------------------------------------------------------------- session (JWT)

function setSession(j) {
  state.token = j.token; state.user = j.user;
  store.set("netlab.jwt", j.token);
  $("#userName").textContent = j.user;
}

// every request that hits 401 waits on the same sign-in screen
let pendingLogin = null;
function login(message = "") {
  if (pendingLogin) return pendingLogin;
  pendingLogin = new Promise((resolve) => {
    const form = $("#loginForm"), err = $("#loginError"), btn = $("#loginSubmit");
    err.textContent = message;
    $("#login").hidden = false;
    $("#loginUser").focus();
    form.onsubmit = async (e) => {
      e.preventDefault();
      btn.disabled = true;
      try {
        const r = await fetch("/api/login", {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ username: $("#loginUser").value.trim(), password: $("#loginPass").value }),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) { err.textContent = j.detail || "Sign-in failed."; $("#loginPass").select(); return; }
        setSession(j);
        $("#loginPass").value = "";
        $("#login").hidden = true;
        form.onsubmit = null; pendingLogin = null;
        resolve();
        if (j.default_password) changePassword(true);
      } catch { err.textContent = "Cannot reach the server."; }
      finally { btn.disabled = false; }
    };
  });
  return pendingLogin;
}

function signOut() {
  store.del("netlab.jwt");
  location.reload();
}

async function changePassword(isDefault = false) {
  const cur = el("input", { type: "password", autocomplete: "current-password" });
  const nw = el("input", { type: "password", autocomplete: "new-password", minlength: 8 });
  const rep = el("input", { type: "password", autocomplete: "new-password" });
  const row = (label, input) => el("label", { class: "stack" }, label, input);
  if (isDefault) cur.value = "netlab";
  const ok = await modal({ title: "Change password", ok: "Change password", focus: isDefault ? nw : undefined,
    body: el("div", {},
      isDefault ? el("p", { text: "You signed in with the default password (netlab). Anyone who can reach this "
        + "server can guess it, so set your own now." }) : "",
      el("p", { text: "At least 8 characters. Other signed-in sessions for this user are signed out." }),
      row("Current password", cur), row("New password", nw), row("Repeat new password", rep)) });
  if (!ok) return;
  if (nw.value !== rep.value) return toast("The new passwords do not match.");
  setSession(await api("POST", "/password", { current: cur.value, new: nw.value }));
  toast("Password changed.");
}

// ---------------------------------------------------------------- AI assistant panel

// sessions: terminals in the panel; mode "chat" runs the agent on one lab, "login"/"logout" manage sign-in
const ai = { open: store.get("netlab.ai.open") === "1", pick: store.get("netlab.ai.pick") || "claude",
  providers: null, sessions: [] };

async function aiLoadProviders() {
  try { ai.providers = (await api("GET", "/ai")).providers; } catch { ai.providers = ai.providers || []; }
  renderAI();
}

function aiToggle() {
  ai.open = !ai.open;
  store.set("netlab.ai.open", ai.open ? "1" : "0");
  if (ai.open && !ai.providers) aiLoadProviders();
  renderAI();
  window.dispatchEvent(new Event("resize"));  // the sheet and the dock change width
}

function aiCurrent() {
  return ai.sessions.find((c) => c.provider === ai.pick && c.mode !== "chat")
    || ai.sessions.find((c) => c.provider === ai.pick && c.mode === "chat" && c.lab === state.lab?.id);
}

// 9Router: Claude Code through a 9Router proxy; "signing in" stores this user's API key and model
async function aiRouterConnect() {
  const p = ai.providers?.find((x) => x.key === "9router");
  const url = el("input", { type: "url", value: p?.url || "http://127.0.0.1:20128" });
  const key = el("input", { type: "password", autocomplete: "off", placeholder: "from 9Router dashboard → Endpoint" });
  const model = el("input", { type: "text", value: p?.model || "", placeholder: "e.g. cc/claude-sonnet-4-5 or a combo name" });
  const row = (label, input) => el("label", { class: "stack" }, label, input);
  const ok = await modal({ title: "Connect 9Router", ok: "Connect", focus: key,
    body: el("div", {},
      el("p", { text: "Connect providers and create an API key in the 9Router dashboard (port 20128), then enter "
        + "them here. The key and model are yours only; other NetLab users connect their own." }),
      row("9Router URL", url), row("API key", key), row("Model or combo", model)) });
  if (!ok) return;
  ai.providers = (await api("PUT", "/ai/9router", { url: url.value, key: key.value, model: model.value })).providers;
  toast("9Router connected.");
  renderAI();
}

async function aiRouterDisconnect() {
  ai.providers = (await api("DELETE", "/ai/9router")).providers;
  renderAI();
}

function aiStart(mode) {
  const p = ai.providers?.find((x) => x.key === ai.pick);
  if (!p) return;
  if (p.key === "9router" && mode !== "chat") return mode === "login" ? aiRouterConnect() : aiRouterDisconnect();
  if (mode === "chat" && !state.lab) return toast("Open a lab first.");
  ai.sessions.filter((c) => c.provider === p.key && c.mode !== "chat").forEach(aiDispose);
  const url = mode === "chat" ? `/ws/ai/${p.key}?mode=chat&lab=${state.lab.id}` : `/ws/ai/${p.key}?mode=${mode}`;
  const c = makeTerm({ provider: p.key, mode, lab: mode === "chat" ? state.lab.id : null, url }, $("#aiTerms"),
    () => (mode === "chat" ? renderAI() : aiLoadProviders()));
  ai.sessions.push(c);
  renderAI();
  requestAnimationFrame(() => c.term.focus());
}

function aiDispose(c) {
  c.ws.close(); c.term.dispose(); c.box.remove();
  ai.sessions = ai.sessions.filter((x) => x !== c);
  renderAI();
}

function renderAI() {
  $("#aiPanel").hidden = !ai.open;
  document.body.classList.toggle("ai-open", ai.open);
  $("#aiBtn").classList.toggle("on", ai.open);
  $("#aiBtn").setAttribute("aria-expanded", String(ai.open));
  if (!ai.open) return;
  const list = ai.providers || [];
  const p = list.find((x) => x.key === ai.pick);
  const live = (c) => c.ws.readyState <= 1;
  $("#aiPick").replaceChildren(...(list.length ? list : [{ key: ai.pick, label: "Loading…" }]).map((x) => {
    const running = ai.sessions.some((c) => c.provider === x.key && c.mode === "chat" && live(c));
    return el("option", { value: x.key, text: x.label + (running ? " (running)" : ""), selected: x.key === ai.pick });
  }));
  $("#aiStatus").textContent = !p ? "Checking sign-in…" : !p.installed ? "Not installed"
    : p.key === "9router" ? (p.signed_in ? `Connected · ${p.model}` : "Not connected")
    : p.signed_in ? "Signed in" : p.key === "opencode" ? "Free models" : "Not signed in";

  const cur = aiCurrent();
  for (const c of ai.sessions) c.box.style.visibility = c === cur ? "visible" : "hidden";
  const btn = (text, fn, cls) => el("button", { text, class: cls, onclick: fn, disabled: !p?.installed });
  const acts = [];
  if (cur && cur.mode !== "chat") acts.push(btn(live(cur) ? "Cancel" : "Done", () => aiDispose(cur)));
  else {
    const [inText, outText] = p?.key === "9router" ? ["Connect", "Disconnect"] : ["Sign in", "Sign out"];
    acts.push(p?.signed_in ? btn(outText, () => aiStart("logout")) : btn(inText, () => aiStart("login")));
    if (p?.key === "9router" && p.signed_in) acts.unshift(btn("Change", aiRouterConnect));
    if (cur) acts.push(btn("Restart", () => { aiDispose(cur); aiStart("chat"); }), btn("Stop", () => aiDispose(cur)));
  }
  $("#aiActions").replaceChildren(...acts);

  const empty = $("#aiEmpty");
  empty.hidden = !!cur;
  if (!cur) {
    const start = btn(state.lab ? `Start on ${state.lab.name}` : "Open a lab first", () => aiStart("chat"), "accent");
    if (!state.lab) start.disabled = true;
    empty.replaceChildren(
      el("p", { text: p ? p.account : "" }),
      p && !p.installed ? el("p", {}, "Install it on the server: ", el("code", { text: p.install })) : start,
      el("p", { class: "note", text: "The assistant works on the open lab: it reads the topology, runs commands on the "
        + "nodes and can start or stop them. It asks before running commands unless you allow it." }),
      el("p", { class: "note", text: "Sign-in is stored on the NetLab server per NetLab user: other users "
        + "do not see your account or your sessions. Each running assistant needs about 400 MB of free RAM." }));
  } else requestAnimationFrame(() => cur.fit.fit());
}

$("#aiPick").addEventListener("change", (e) => {
  ai.pick = e.target.value;
  store.set("netlab.ai.pick", ai.pick);
  renderAI();
  aiCurrent()?.term.focus();
});

// drag the left edge of the panel to resize it
$("#aiGrip").addEventListener("pointerdown", (e) => {
  const grip = e.target; grip.setPointerCapture(e.pointerId);
  const move = (ev) => {
    const w = Math.max(320, Math.min(window.innerWidth - 480, window.innerWidth - ev.clientX));
    document.documentElement.style.setProperty("--ai-w", w + "px");
    window.dispatchEvent(new Event("resize"));
  };
  grip.addEventListener("pointermove", move);
  grip.addEventListener("pointerup", () => grip.removeEventListener("pointermove", move), { once: true });
});

// ---------------------------------------------------------------- updates

let updateInfo = null;

async function checkUpdate(force = false) {
  try { updateInfo = await api("GET", "/update" + (force ? "?force=1" : "")); } catch { return; }
  $("#version").textContent = "v" + updateInfo.version;
  const btn = $("#updateBtn");
  btn.hidden = !updateInfo.available;
  btn.textContent = `Update to ${updateInfo.latest}`;
}

async function showUpdate() {
  await checkUpdate(true);
  const u = updateInfo;
  if (!u) return;
  if (u.error && /token/.test(u.error)) return askToken();
  if (u.error) return toast("Could not check for updates: " + u.error);
  if (!u.available) return toast(`NetLab ${u.version} is up to date.`);
  const body = el("div", {},
    el("p", { text: `NetLab ${u.latest} is available. Installed: ${u.version}.` }),
    u.notes ? el("p", { class: "notes", text: u.notes }) : "",
    u.changes.length ? el("ul", { class: "changes" }, ...u.changes.map((c) => el("li", { text: c }))) : "",
    el("p", { class: "note", text: "The web page reloads when the update is done. Running labs and VMs keep running, "
      + "and labs, images and users are kept. If the new version does not start, NetLab goes back to this one." }));
  if (!(await modal({ title: "Update NetLab", body, ok: `Update to ${u.latest}`, wide: true }))) return;
  await api("POST", "/update", { version: u.latest });
  followUpdate(u.version);
}

// the NetLab repository is private: updates need a read-only GitHub token, kept on the server
async function askToken() {
  const input = el("input", { type: "password", autocomplete: "off", placeholder: "github_pat_…" });
  const body = el("div", {},
    el("p", { text: "Updates come from a private GitHub repository. Paste a GitHub token that can read it "
      + "(fine-grained token, Contents: Read-only)." }), input,
    el("p", { class: "note", text: "The token stays on this server and is used only to download NetLab releases." }));
  if (!(await modal({ title: "GitHub token for updates", body, ok: "Save and check" }))) return;
  try { updateInfo = await api("PUT", "/update/token", { token: input.value.trim() }); }
  catch { return; }  // api() already showed the error
  if (updateInfo.error) return toast("GitHub still refuses access: " + updateInfo.error);
  checkUpdate();
  showUpdate();
}

// the server restarts during the update, so poll the sign-in-free status until it settles
async function followUpdate(from) {
  const t = $("#toast");
  t.hidden = false;
  clearTimeout(toastTimer);
  for (;;) {
    await new Promise((r) => setTimeout(r, 2000));
    let s;
    try { s = await (await fetch("/api/update/status", { cache: "no-store" })).json(); }
    catch { t.textContent = "Updating NetLab: restarting…"; continue; }
    if (s.state === "running") t.textContent = `Updating NetLab: ${s.step || "working"}…`;
    else if (s.state === "done" && s.version !== from) { t.textContent = `Updated to ${s.version}. Reloading…`; location.reload(); return; }
    else if (s.state === "failed") { t.hidden = true; toast("Update failed: " + s.error); checkUpdate(); return; }
  }
}

// ---------------------------------------------------------------- VM images

const fmtSize = (b) => (b >= 2 ** 30 ? `${(b / 2 ** 30).toFixed(1)} GB` : `${Math.max(1, Math.round(b / 2 ** 20))} MB`);
const slug = (s) => s.toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^[^a-z0-9]+|-+$/g, "");
let upload = null;  // the upload in progress: {xhr, label}

// guess the catalog device and version from a download's file name, e.g. vyos-1.4.0-generic-amd64.iso
const FILE_HINTS = [[/vios.?l2/i, "viosl2"], [/vios/i, "vios"], [/c8000v/i, "c8000v"], [/(^|[^a-z])chr([^a-z]|$)|mikrotik|routeros/i, "mikrotik"],
  [/vyos/i, "vyos"], [/openwrt/i, "openwrt"], [/cumulus/i, "cumulus"], [/opnsense/i, "opnsense"], [/pfsense/i, "pfsense"],
  [/fgt|fortigate|fortios/i, "fortigate"], [/asav/i, "asav"], [/vsrx/i, "vsrx"], [/pa-vm|paloalto|pan-os/i, "paloalto"],
  [/windows|win(7|10|11|20\d\d)/i, "windows"]];
const guessDevice = (fname) => FILE_HINTS.find(([re]) => re.test(fname))?.[1];
const guessVersion = (fname) => fname.match(/\d+(?:\.\d+)+/)?.[0] || "";

async function manageImages(key) {
  const info = await api("GET", "/images");
  const opt = (value, text) => el("option", { value, text });
  const field = (label, input) => el("label", { class: "stack" }, label, input);
  const device = el("select", {}, ...state.tpl.qemu_catalog.map((c) => opt(c.key, `${c.label} (${GROUPS.find((g) => g[0] === c.category)?.[1] || c.category})`)),
    opt("", "Other (custom image)"));
  device.value = key ?? "mikrotik";
  const version = el("input", { type: "text", placeholder: "for example 7.25" });
  const label = el("input", { type: "text", placeholder: "for example Arista vEOS" });
  const category = el("select", {}, ...GROUPS.filter(([c]) => c !== "cloud").map(([c, t]) => opt(c, t)));
  const ram = el("input", { type: "number", min: 64, max: 65536, value: 512 });
  const ifaces = el("input", { type: "number", min: 1, max: 24, value: 4 });
  const nic = el("select", {}, opt("virtio-net-pci", "virtio"), opt("e1000", "e1000 (Cisco, MikroTik, Windows)"), opt("vmxnet3", "vmxnet3"));
  const diskIf = el("select", {}, opt("virtio", "virtio"), opt("ide", "IDE"), opt("scsi", "SCSI"));
  const file = el("input", { type: "file", accept: ".qcow2,.img,.raw,.vmdk,.vhd,.vhdx,.vdi,.iso" });
  const diskGb = el("input", { type: "number", min: 1, max: 512, value: 8 });
  const isoBox = el("div", { class: "grid2" }, field("Install disk (GB)", diskGb),
    el("p", { class: "note", text: "An .iso boots from CD with this empty disk attached. Install it to the disk (VyOS: install image) and later boots start from the disk. Wipe a node to start over." }));
  let deviceTouched = key !== undefined, autoVersion = "";
  device.addEventListener("change", () => { deviceTouched = true; });
  file.onchange = () => {
    const f = file.files[0];
    isoBox.hidden = !f || !/\.iso$/i.test(f.name);
    if (!f) return;
    const dev = guessDevice(f.name), ver = guessVersion(f.name);
    if (!deviceTouched && dev) { device.value = dev; sync(); }
    if (ver && (!version.value || version.value === autoVersion)) version.value = autoVersion = ver;
  };
  const custom = el("div", { class: "grid2" }, field("Name", label), field("Group", category), field("RAM (MB)", ram),
    field("Interfaces", ifaces), field("Network card", nic), field("Disk bus", diskIf));
  function sync() { custom.hidden = device.value !== ""; }
  device.onchange = sync; sync(); isoBox.hidden = true;

  const rows = info.images.map((m) => {
    const del = el("button", { type: "button", class: "danger", text: "Delete", disabled: m.used_by.length > 0,
      title: m.used_by.length ? "Used by " + m.used_by.join(", ") : "" });
    del.onclick = async () => {
      if (!confirm(`Delete image ${m.name}? This cannot be undone.`)) return;
      await api("DELETE", "/images/" + encodeURIComponent(m.name));
      del.closest("tr").remove();
      await reloadTemplates();
    };
    return el("tr", {}, el("td", {}, el("b", { text: m.label }), el("div", { class: "note", text: m.name + (m.iso ? " · ISO" : "") })),
      el("td", { class: "num", text: fmtSize(m.size) }),
      el("td", { class: "note", text: m.used_by.length ? "in " + m.used_by.join(", ") : "not used" }), el("td", {}, del));
  });
  const body = el("div", { class: "images" },
    el("h3", { text: "Upload" }),
    el("p", { text: `Accepts .qcow2, .img, .raw, .vmdk, .vhd, .vhdx, .vdi and .iso. Disks that are not qcow2 are converted to qcow2. Unzip .zip downloads first. Device and version are filled in from the file name; check them before uploading. Server disk: ${fmtSize(info.free)} free, ${fmtSize(info.reserve)} kept in reserve.` }),
    field("Disk file", file), el("div", { class: "grid2" }, field("Device", device), field("Version", version)), isoBox, custom,
    el("h3", { text: "Images on the server" }),
    rows.length ? el("table", { class: "imgs" }, ...rows) : el("p", { text: "No images yet." }));
  if (upload) file.disabled = true;

  while (await modal({ title: "VM images", body, ok: "Upload", wide: true, focus: file })) {
    const err = (m) => { toast(m); return null; };
    const f = file.files[0];
    const ver = slug(version.value.trim());
    const isCustom = device.value === "";
    let name, meta = {};
    if (upload) { err("An upload is already running."); continue; }
    if (!f) { err("Choose a disk file."); continue; }
    if (isCustom) {
      if (!label.value.trim()) { err("Give the custom image a name."); continue; }
      name = slug(label.value) + (ver ? "-" + ver : "");
      meta = { label: (label.value.trim() + " " + version.value.trim()).trim(), category: category.value,
        ram: +ram.value, ifaces: +ifaces.value, nic: nic.value, disk_if: diskIf.value };
    } else {
      if (!ver) { err("Enter the version, for example 7.25."); continue; }
      name = `${device.value}-${ver}`;
    }
    if (info.images.some((m) => m.name === name)) { err(`Image ${name} already exists. Delete it first, or use another version.`); continue; }
    if (f.size > info.free - info.reserve) { err(`Not enough disk space for ${fmtSize(f.size)}.`); continue; }
    startUpload(name, f, meta, +diskGb.value || 8);
    return;
  }
}

function startUpload(name, f, meta, diskGb) {
  const box = $("#upload"), bar = box.querySelector("progress"), text = box.querySelector("span");
  const xhr = new XMLHttpRequest();
  upload = { xhr };
  const done = (msg) => { upload = null; box.hidden = true; if (msg) toast(msg); };
  xhr.open("PUT", `/api/images/${encodeURIComponent(name)}?filename=${encodeURIComponent(f.name)}&meta=${encodeURIComponent(JSON.stringify(meta))}&disk_gb=${diskGb}`);
  xhr.setRequestHeader("authorization", "Bearer " + state.token);
  xhr.setRequestHeader("content-type", "application/octet-stream");
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    bar.value = e.loaded / e.total;
    text.textContent = e.loaded < e.total ? `Uploading ${name}: ${fmtSize(e.loaded)} of ${fmtSize(e.total)}`
      : `Checking and converting ${name}…`;
  };
  xhr.onload = async () => {
    let j = {}; try { j = JSON.parse(xhr.responseText); } catch {}
    if (xhr.status === 401) return done("Your session expired during the upload. Sign in and upload again.");
    if (xhr.status >= 300) return done("Upload failed: " + (j.detail || xhr.statusText));
    done(`${j.label || name} is ready. Drag it from the Device list onto the lab.`);
    await reloadTemplates();
  };
  xhr.onerror = () => done("Upload failed: the connection to the server was lost.");
  xhr.onabort = () => done("Upload cancelled.");
  bar.value = 0;
  text.textContent = `Uploading ${name}…`;
  box.hidden = false;
  xhr.send(f);
}

async function reloadTemplates() {
  state.tpl = await api("GET", "/templates");
  renderPalette();
}

// ---------------------------------------------------------------- labs

async function loadLabs(selectId) {
  state.labs = await api("GET", "/labs");
  const sel = $("#labSelect");
  sel.innerHTML = "";
  for (const l of state.labs) {  // admin sees every user's labs: mark the ones that are not theirs
    const who = l.owner && l.owner !== state.user ? `  · ${l.owner}` : "";
    sel.append(el("option", { value: l.id, text: `${l.name}  (${l.nodes})${who}` }));
  }
  const id = selectId || store.get("netlab.lab");
  const pick = state.labs.find((l) => l.id === id) || state.labs[0];
  if (pick) { sel.value = pick.id; await openLab(pick.id); }
  else { state.lab = null; render(); }
}

// one-shot cable animations: remember when, and re-render once they are over
const LINK_ANIM_MS = 900;
function absorbLinkStatus(ls) {
  const now = performance.now();
  for (const [id, s] of Object.entries(ls || {})) {
    if (s === "up" && state.linkStatus[id] === "down") state.linkUpAt.set(id, now);
  }
  state.linkStatus = ls || {};
  setTimeout(render, LINK_ANIM_MS + 50);
}

async function openLab(id) {
  state.lab = await api("GET", `/labs/${id}`);
  state.status = state.lab.status || {};
  state.linkStatus = state.lab.link_status || {};
  state.linkBornAt.clear(); state.linkUpAt.clear();
  aiSeed(state.lab.ai); state.aiActive = new Set();
  state.sel = null;
  store.set("netlab.lab", id);
  state.zoom = Number(store.get("netlab.zoom." + id)) || 1;
  applyZoom();
  render();
  renderAI();
}

async function reloadLab() {
  if (!state.lab) return;
  const before = new Set(Object.keys(state.lab.links));
  state.lab = await api("GET", `/labs/${state.lab.id}`);
  state.status = state.lab.status || {};
  for (const id of Object.keys(state.lab.links)) if (!before.has(id)) state.linkBornAt.set(id, performance.now());
  absorbLinkStatus(state.lab.link_status);
  render();
}

const actions = {
  async newLab() {
    const name = await ask("New lab", "Lab " + (state.labs.length + 1));
    if (!name) return;
    const lab = await api("POST", "/labs", { name });
    await loadLabs(lab.id);
  },
  async demoLab() {
    const lab = await api("POST", "/labs", { demo: true });
    await loadLabs(lab.id);
    toast("Demo lab created. Press Start all, then open PC1 and ping 192.168.3.10.");
  },
  async renameLab() {
    if (!state.lab) return;
    const name = await ask("Rename lab", state.lab.name);
    if (!name) return;
    await api("PATCH", `/labs/${state.lab.id}`, { name });
    await loadLabs(state.lab.id);
  },
  async deleteLab() {
    if (!state.lab) return;
    const ok = await modal({ title: `Delete ${state.lab.name}?`, ok: "Delete",
      body: el("p", { text: "All nodes are stopped and their saved configs are removed." }) });
    if (!ok) return;
    closeConsoles((c) => c.lab === state.lab.id);
    ai.sessions.filter((c) => c.lab === state.lab.id).forEach(aiDispose);
    await api("DELETE", `/labs/${state.lab.id}`);
    store.set("netlab.lab", "");
    await loadLabs();
  },
  exportLab() {
    if (!state.lab) return;
    const { name, seq, nodes, networks, links } = state.lab;
    const blob = new Blob([JSON.stringify({ name, seq, nodes, networks, links }, null, 2)], { type: "application/json" });
    const a = el("a", { href: URL.createObjectURL(blob), download: `${name.replace(/\W+/g, "_")}.json` });
    a.click(); URL.revokeObjectURL(a.href);
  },
  importLab() { $("#importFile").click(); },
  async startLab() {
    if (!state.lab) return;
    toast("Starting all nodes…");
    const r = await api("POST", `/labs/${state.lab.id}/start`);
    const errs = Object.entries(r.errors || {});
    if (errs.length) toast(errs.map(([n, e]) => `${state.lab.nodes[n].name}: ${e}`).join(" · "));
    else toast("All nodes started.");
    await reloadLab();
  },
  async stopLab() {
    if (!state.lab) return;
    await api("POST", `/labs/${state.lab.id}/stop`);
    await reloadLab();
  },
  changePassword,
  signOut,
  toggleAI: aiToggle,
  showUpdate,
  manageImages() { manageImages(); },
  cancelUpload() { if (upload && confirm("Cancel the upload?")) upload.xhr.abort(); },
  cancelMode() { setMode(null); },
  toggleConnect() { setMode(state.mode?.connect ? null : { connect: true, from: null }); },
};

$("#importFile").addEventListener("change", async (e) => {
  const f = e.target.files[0]; e.target.value = "";
  if (!f) return;
  try {
    const data = JSON.parse(await f.text());
    const lab = await api("POST", "/labs", { name: data.name || f.name, import: data });
    await loadLabs(lab.id);
  } catch (err) { toast("Import failed: " + err.message); }
});
$("#labSelect").addEventListener("change", (e) => openLab(e.target.value));
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-act]");
  if (b && actions[b.dataset.act]) actions[b.dataset.act]();
});

// ---------------------------------------------------------------- palette

const knownCategory = (c) => (CATEGORY_ICON[c] ? c : "other");

function devices() {
  const t = state.tpl.templates;
  const out = [
    { category: "router", label: t.frr.label, place: { template: "frr" } },
    { category: "switch", label: "Switch (L2 bridge)", place: { network: true, type: "bridge" } },
    { category: "cloud", label: "NAT Cloud (internet)", place: { network: true, type: "nat" } },
    { category: "host", label: t.linux.label, place: { template: "linux" } },
  ];
  if (state.tpl.qemu_available) {
    for (const img of state.tpl.qemu_images) {
      out.push({ category: knownCategory(img.category), label: img.label || img.name, place: { template: "qemu", image: img.name } });
    }
  }
  for (const b of state.tpl.clouds.host_bridges) {
    out.push({ category: "cloud", label: `Cloud ${b.bridge} (server LAN)`, place: { network: true, type: "pnet", bridge: b.bridge } });
  }
  if (!state.tpl.clouds.host_bridges.length) {
    out.push({ category: "cloud", label: "Cloud pnet0 (server LAN)", missing: {
      sub: "not set up on the server", title: "Needs a pnet0 bridge on the server",
      help: "The server has no pnet0 bridge yet. Follow \"Cloud pnet0\" in the README to bridge the server's network card, then reload this page." } });
  }
  // catalog appliances with no image on the server yet
  const have = new Set(state.tpl.qemu_images.map((m) => m.template));
  for (const c of state.tpl.qemu_catalog) {
    if (have.has(c.key)) continue;
    const ram = c.ram >= 1024 ? `${+(c.ram / 1024).toFixed(1)} GB` : `${c.ram} MB`;
    out.push({ category: knownCategory(c.category), label: c.label, upload: c.key, missing: {
      sub: `no image · ${ram} RAM`, title: `No image yet. Needs ${ram} RAM. Click to upload one.` } });
  }
  return out;
}

const showMissing = () => store.get("netlab.showMissing") !== "0";

function nodeCategory(n) {
  if (n.template !== "qemu") return state.tpl.templates[n.template].category;
  return knownCategory(state.tpl.qemu_images.find((m) => m.name === n.image)?.category);
}

function renderPalette() {
  const box = $("#paletteItems");
  box.innerHTML = "";
  const toggle = el("input", { type: "checkbox", checked: showMissing(),
    onchange: (e) => { store.set("netlab.showMissing", e.target.checked ? "1" : "0"); renderPalette(); } });
  box.append(el("label", { class: "check" }, toggle, "Show devices without an image"));
  const list = devices().filter((d) => !d.missing || showMissing());
  for (const [cat, title] of GROUPS) {
    const items = list.filter((d) => d.category === cat);
    if (!items.length) continue;
    box.append(el("h3", { text: title }));
    for (const d of items) {
      if (d.missing) {
        const b = el("button", { class: "tool missing", title: d.missing.title });
        b.innerHTML = `<svg viewBox="0 0 48 48"><use href="#${CATEGORY_ICON[cat]}"/></svg>`;
        b.append(el("span", {}, d.label, el("small", { text: d.missing.sub })));
        b.onclick = () => (d.upload ? manageImages(d.upload) : toast(d.missing.help));
        box.append(b);
        continue;
      }
      const mode = { place: d.place };
      const b = el("button", { class: "tool", draggable: "true", title: "Drag onto the lab sheet, or click it and then click the sheet" });
      b.innerHTML = `<svg viewBox="0 0 48 48"><use href="#${CATEGORY_ICON[cat]}"/></svg>`;
      b.append(el("span", { text: d.label }));
      b.onclick = () => setMode(mode);
      b.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData(DRAG_TYPE, JSON.stringify(d.place));
        e.dataTransfer.effectAllowed = "copy";
        e.dataTransfer.setDragImage(b.querySelector("svg"), 16, 13);
        setMode(null);
      });
      b._mode = mode;
      box.append(b);
    }
  }
  if (!state.tpl.qemu_available) box.append(el("p", { class: "hint", text: "QEMU is not installed on the server; VM devices are unavailable." }));
}

async function placeDevice(place, p) {
  if (place.network) await api("POST", `/labs/${state.lab.id}/networks`, { ...p, type: place.type, bridge: place.bridge });
  else await api("POST", `/labs/${state.lab.id}/nodes`, { template: place.template, image: place.image, ...p });
  await reloadLab();
}

// drag a device from the palette onto the lab sheet
const stage = $("#stage");
stage.addEventListener("dragover", (e) => {
  if (!state.lab || !e.dataTransfer.types.includes(DRAG_TYPE)) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "copy";
  stage.classList.add("drop");
});
stage.addEventListener("dragleave", (e) => { if (!stage.contains(e.relatedTarget)) stage.classList.remove("drop"); });
stage.addEventListener("drop", async (e) => {
  stage.classList.remove("drop");
  const data = e.dataTransfer.getData(DRAG_TYPE);
  if (!data || !state.lab) return;
  e.preventDefault();
  const p = canvasPoint(e), snap = (v) => Math.max(40, Math.round(v / 12) * 12);
  try { await placeDevice(JSON.parse(data), { x: snap(p.x), y: snap(p.y) }); } catch {}
});

function setMode(mode) {
  state.mode = mode;
  if (mode?.connect) mode.from = null;
  for (const b of document.querySelectorAll("#paletteItems .tool")) b.classList.toggle("on", !!mode?.place && JSON.stringify(b._mode) === JSON.stringify(mode));
  $("#connectBtn").classList.toggle("on", !!mode?.connect);
  $("#canvas").classList.toggle("placing", !!mode?.place);
  $("#canvas").classList.toggle("connecting", !!mode?.connect);
  $("#ghost").setAttribute("visibility", "hidden");
  $("#modeHint").textContent = mode?.place ? "Click the canvas to place it. Esc cancels."
    : mode?.connect ? "Click a node, then a node or switch. The next free interface is used." : "";
  render();
}

// ---------------------------------------------------------------- canvas

function svg(tag, attrs = {}) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}

function endPos(end) {
  const o = end.node ? state.lab.nodes[end.node] : state.lab.networks[end.network];
  return o ? { x: o.x, y: o.y } : { x: 0, y: 0 };
}

// QEMU images name their ports: management ports from iface_names, then iface_prefix + number
// (MikroTik ether1.., FortiGate port1.., ASAv Management0/0 then Gi0/0..)
function ifName(nodeId, i, short = false) {
  const n = state.lab.nodes[nodeId];
  const img = n?.image && state.tpl.qemu_images.find((m) => m.name === n.image);
  const named = img?.iface_names || [];
  if (i < named.length) return short ? named[i].slice(0, 4) : named[i];
  const prefix = img?.iface_prefix || "eth", num = i - named.length + (img?.iface_start || 0);
  return short ? prefix[0] + num : prefix + num;
}

function endName(end) {
  if (end.network) return state.lab.networks[end.network]?.name || "?";
  return `${state.lab.nodes[end.node]?.name || "?"} ${ifName(end.node, end.iface)}`;
}

function renderModeBar() {
  const m = state.mode, bar = $("#modeBar");
  bar.hidden = !m;
  if (!m) return;
  const from = m.from && (m.from.node ? state.lab?.nodes[m.from.node] : state.lab?.networks[m.from.network]);
  bar.querySelector("span").textContent = m.place ? "Click the lab sheet to place the device."
    : from ? `Connect from ${from.name}: click the device to connect to.` : "Connect: click the first device.";
}

function render() {
  renderModeBar();
  const lab = state.lab;
  $("#empty").hidden = !!lab;
  $("#zoomBar").hidden = !lab;
  $("#labCount").textContent = lab
    ? `${Object.values(state.status).filter((s) => s === "running").length} / ${Object.keys(lab.nodes).length} running` : "";
  const links = $("#linkLayer"), nodes = $("#nodeLayer");
  links.innerHTML = ""; nodes.innerHTML = "";
  renderInspector();
  if (!lab) return;

  const now = performance.now();
  const recent = (m, id) => { const t = m.get(id); return t !== undefined && now - t < LINK_ANIM_MS ? now - t : null; };
  // links between the same two devices are drawn side by side, 12 units apart
  const endKey = (e) => (e.node ? "n" + e.node : "w" + e.network);
  const pairKey = (l) => [endKey(l.a), endKey(l.b)].sort().join("|");
  const pairs = new Map();
  for (const l of Object.values(lab.links)) pairs.set(pairKey(l), [...(pairs.get(pairKey(l)) || []), l.id]);
  for (const l of Object.values(lab.links)) {
    let a = endPos(l.a), b = endPos(l.b);
    const group = pairs.get(pairKey(l));
    if (group.length > 1) {
      // perpendicular to the pair's direction, measured the same way for every link in the group
      const [p0, p1] = endKey(l.a) < endKey(l.b) ? [a, b] : [b, a];
      const len = Math.hypot(p1.x - p0.x, p1.y - p0.y) || 1;
      const off = (group.indexOf(l.id) - (group.length - 1) / 2) * 12;
      const nx = -(p1.y - p0.y) / len * off, ny = (p1.x - p0.x) / len * off;
      a = { x: a.x + nx, y: a.y + ny };
      b = { x: b.x + nx, y: b.y + ny };
    }
    const xy = { x1: a.x, y1: a.y, x2: b.x, y2: b.y };
    const up = state.linkStatus[l.id] === "up";
    const born = recent(state.linkBornAt, l.id), cameUp = recent(state.linkUpAt, l.id);
    const g = svg("g", { class: `link ${up ? "up" : "down"}${isSel("link", l.id) ? " sel" : ""}${born !== null ? " fresh" : ""}` });
    // negative delays keep animations continuous although render() rebuilds the elements
    const wire = svg("line", { class: "wire", ...xy, ...(born !== null ? { pathLength: 1 } : {}) });
    if (born !== null) wire.style.animationDelay = `-${born}ms`;
    g.append(wire);
    if (up) {
      const flow = svg("line", { class: "flow", ...xy });
      flow.style.animationDelay = `-${now % 1200}ms`;
      g.append(flow);
    }
    if (up && cameUp !== null) {
      const pulse = svg("line", { class: "pulse", ...xy, pathLength: 1 });
      pulse.style.animationDelay = `-${cameUp}ms`;
      g.append(pulse);
    }
    for (const [p, q, end] of [[a, b, l.a], [b, a, l.b]]) {  // port status dot just outside the icon
      if (!end.node) continue;
      const d = Math.hypot(q.x - p.x, q.y - p.y) || 1;
      g.append(svg("circle", { class: "dot", r: 4.5, cx: p.x + (q.x - p.x) * 31 / d, cy: p.y + (q.y - p.y) * 31 / d }));
    }
    const hit = svg("line", { class: "hit", x1: a.x, y1: a.y, x2: b.x, y2: b.y });
    hit.addEventListener("pointerdown", (e) => { e.stopPropagation(); select("link", l.id); });
    g.append(hit);
    // interface names sit near each end, like annotations on a drawing
    const lab1 = (p, q, end) => {
      if (!end.node) return;
      const d = Math.hypot(q.x - p.x, q.y - p.y) || 1, k = Math.min(60, d * 0.4) / d;
      const t = svg("text", { x: p.x + (q.x - p.x) * k, y: p.y + (q.y - p.y) * k + 3, "text-anchor": "middle" });
      t.textContent = ifName(end.node, end.iface, true);
      g.append(t);
    };
    lab1(a, b, l.a); lab1(b, a, l.b);
    links.append(g);
  }

  const drawThing = (kind, o, icon, running, folio) => {
    const g = svg("g", {
      class: `node${running ? " running" : ""}${isSel(kind, o.id) ? " sel" : ""}${pickedFrom(kind, o.id) ? " pick" : ""}`
        + (kind === "node" && state.aiActive.has(o.id) ? " ai" : ""),
      transform: `translate(${o.x},${o.y})`, "data-kind": kind, "data-id": o.id,
    });
    g.append(svg("rect", { class: "frame", x: -34, y: -34, width: 68, height: 84 }));
    g.append(svg("rect", { class: "pad", x: -24, y: -24, width: 48, height: 48 }));
    g.append(svg("use", { href: `#${icon}`, class: "icon", x: -24, y: -24, width: 48, height: 48 }));
    const label = svg("text", { class: "label", x: 0, y: 42 });
    label.textContent = o.name;
    g.append(label);
    if (folio) {
      const f = svg("text", { class: "folio", x: 30, y: -14 });
      f.textContent = String(folio).padStart(2, "0");
      g.append(f);
    }
    // connector: drag it onto another device to create a link
    const plug = svg("g", { class: "plug", transform: "translate(24,20)" });
    const tip = svg("title");
    tip.textContent = "Drag to another device to connect";
    plug.append(tip, svg("circle", { r: 9 }), svg("path", { d: "M-4-1h8v1.5a4 4 0 0 1-8 0zM-2-1v-3.5M2-1v-3.5M0 4.5V7" }));
    plug.addEventListener("pointerdown", (e) => startLinkDrag(e, kind, o));
    g.append(plug);
    g.addEventListener("pointerdown", (e) => onThingDown(e, kind, o));
    nodes.append(g);
  };
  for (const n of Object.values(lab.networks)) drawThing("network", n, (n.type || "bridge") === "bridge" ? "ico-switch" : "ico-cloud", false, null);
  for (const n of Object.values(lab.nodes)) {
    drawThing("node", n, CATEGORY_ICON[nodeCategory(n)], state.status[n.id] === "running", n.id);
  }
}

const isSel = (kind, id) => state.sel?.kind === kind && state.sel.id === id;
const pickedFrom = (kind, id) => state.mode?.connect && state.mode.from &&
  ((kind === "node" && state.mode.from.node === id) || (kind === "network" && state.mode.from.network === id));

function select(kind, id) { state.sel = kind ? { kind, id } : null; render(); }

function canvasPoint(e) {  // client coordinates -> topology coordinates (independent of zoom)
  const r = $("#canvas").getBoundingClientRect();
  return { x: Math.round((e.clientX - r.left) / state.zoom), y: Math.round((e.clientY - r.top) / state.zoom) };
}

// ---------------------------------------------------------------- zoom

const CANVAS_W = 2400, CANVAS_H = 1600, ZOOM_MIN = 0.25, ZOOM_MAX = 3;
state.zoom = 1;

function applyZoom() {
  const z = state.zoom, c = $("#canvas");
  c.setAttribute("width", CANVAS_W * z);
  c.setAttribute("height", CANVAS_H * z);
  $("#stage").style.backgroundSize = `${24 * z}px ${24 * z}px`;  // the grid scales with the drawing
  $("#zoomLevel").textContent = Math.round(z * 100) + "%";
}

// anchor (client x/y) stays under the same point of the drawing; default is the view centre
function setZoom(z, anchor) {
  z = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z));
  const st = $("#stage"), r = st.getBoundingClientRect();
  const ax = anchor ? anchor.x - r.left : st.clientWidth / 2;
  const ay = anchor ? anchor.y - r.top : st.clientHeight / 2;
  const cx = (st.scrollLeft + ax) / state.zoom, cy = (st.scrollTop + ay) / state.zoom;
  state.zoom = z;
  applyZoom();
  st.scrollLeft = cx * z - ax;
  st.scrollTop = cy * z - ay;
  if (state.lab) store.set("netlab.zoom." + state.lab.id, String(z));
}

function zoomStep(dir) {  // to the next of a fixed set of levels
  const levels = [0.25, 0.33, 0.5, 0.67, 0.8, 1, 1.25, 1.5, 2, 2.5, 3];
  const z = state.zoom;
  setZoom(dir > 0 ? levels.find((l) => l > z + 0.001) ?? ZOOM_MAX : [...levels].reverse().find((l) => l < z - 0.001) ?? ZOOM_MIN);
}

function zoomToFit() {
  const lab = state.lab;
  const items = lab ? [...Object.values(lab.nodes), ...Object.values(lab.networks)] : [];
  if (!items.length) return setZoom(1);
  const xs = items.map((o) => o.x), ys = items.map((o) => o.y), pad = 80;
  const x0 = Math.max(0, Math.min(...xs) - pad), x1 = Math.max(...xs) + pad;
  const y0 = Math.max(0, Math.min(...ys) - pad), y1 = Math.max(...ys) + pad;
  const st = $("#stage");
  const z = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.min(st.clientWidth / (x1 - x0), st.clientHeight / (y1 - y0), 1.5)));
  state.zoom = z;
  applyZoom();
  st.scrollLeft = x0 * z - (st.clientWidth - (x1 - x0) * z) / 2;
  st.scrollTop = y0 * z - (st.clientHeight - (y1 - y0) * z) / 2;
  store.set("netlab.zoom." + lab.id, String(z));
}

Object.assign(actions, {
  zoomIn: () => zoomStep(1),
  zoomOut: () => zoomStep(-1),
  zoomReset: () => setZoom(1),
  zoomFit: zoomToFit,
});

// ctrl/cmd + wheel, and trackpad pinch (which browsers report as ctrl + wheel)
$("#stage").addEventListener("wheel", (e) => {
  if (!(e.ctrlKey || e.metaKey) || !state.lab) return;
  e.preventDefault();
  setZoom(state.zoom * Math.exp(-e.deltaY * 0.002), { x: e.clientX, y: e.clientY });
}, { passive: false });

// ---------------------------------------------------------------- click menu

function closeMenu() { $("#ctxMenu").hidden = true; }

// opens beside the device, never under the pointer, so the second click of a double-click lands on the
// device (console) and not on a menu item; clicks in the first 400 ms are ignored for the same reason
function showMenu(kind, o) {
  const menu = $("#ctxMenu"), opened = performance.now();
  menu.innerHTML = "";
  const item = (label, fn, cls) => menu.append(el("button", { type: "button", role: "menuitem", text: label, class: cls,
    onclick: () => { if (performance.now() - opened < 400) return; closeMenu(); fn(); } }));
  const rename = async () => {
    const name = await ask(`Rename ${o.name}`, o.name);
    if (!name || name === o.name) return;
    await api("PATCH", `/labs/${state.lab.id}/${kind === "node" ? "nodes" : "networks"}/${o.id}`, { name });
    await reloadLab();
  };
  const connect = () => { setMode({ connect: true, once: true }); state.mode.from = kind === "node" ? { node: o.id } : { network: o.id }; render(); };
  menu.append(el("div", { class: "menu-title", text: o.name }));
  if (kind === "node") {
    const acts = nodeActions(o), del = acts.pop();
    for (const [label, fn, cls] of acts) item(label, fn, cls);
    item("Connect to…", connect);
    item("Rename", rename);
    item(del[0], del[1], del[2]);
  } else {
    item("Connect to…", connect);
    item("Rename", rename);
    item("Delete", () => { select(kind, o.id); deleteSelection(); }, "danger");
  }
  menu.hidden = false;
  const r = menu.getBoundingClientRect();
  const icon = document.querySelector(`#nodeLayer .node[data-kind="${kind}"][data-id="${o.id}"]`).getBoundingClientRect();
  const right = icon.right + 12, left = icon.left - 12 - r.width;
  menu.style.left = (right + r.width <= innerWidth - 8 || left < 8 ? Math.min(right, innerWidth - r.width - 8) : left) + "px";
  menu.style.top = Math.max(8, Math.min(icon.top - 4, innerHeight - r.height - 8)) + "px";
  menu.querySelector("button:not(:disabled)")?.focus({ preventScroll: true });
}

document.addEventListener("pointerdown", (e) => { if (!e.target.closest("#ctxMenu")) closeMenu(); }, true);
window.addEventListener("resize", closeMenu);
$("#stage").addEventListener("scroll", closeMenu);
$("#canvas").addEventListener("contextmenu", (e) => {
  const t = e.target.closest(".node");
  if (!t) return;
  e.preventDefault();
  const kind = t.dataset.kind, o = (kind === "node" ? state.lab.nodes : state.lab.networks)[t.dataset.id];
  select(kind, o.id);
  showMenu(kind, o);
});

let drag = null, lastDown = { id: null, t: 0 };
function onThingDown(e, kind, o) {
  e.stopPropagation();
  if (state.mode?.connect) return connectClick(kind, o);
  // render() replaces the element on every select, so native dblclick is unreliable here
  const key = kind + o.id, now = performance.now();
  if (kind === "node" && lastDown.id === key && now - lastDown.t < 400) {
    lastDown = { id: null, t: 0 };
    closeMenu();
    return openConsole(o);
  }
  lastDown = { id: key, t: now };
  select(kind, o.id);
  const p = canvasPoint(e);
  drag = { kind, o, dx: p.x - o.x, dy: p.y - o.y, moved: false };
  $("#canvas").setPointerCapture(e.pointerId);
}

$("#canvas").addEventListener("pointermove", (e) => {
  const p = canvasPoint(e);
  if (drag) {
    const snap = (v) => Math.round(v / 12) * 12;  // half the background grid
    drag.o.x = Math.max(40, snap(p.x - drag.dx));
    drag.o.y = Math.max(40, snap(p.y - drag.dy));
    drag.moved = true;
    render();
  } else if (linkDrag) {
    const a = endPos(linkDrag.from), g = $("#ghost");
    g.setAttribute("x1", a.x); g.setAttribute("y1", a.y);
    g.setAttribute("x2", p.x); g.setAttribute("y2", p.y);
    g.setAttribute("visibility", "visible");
    const t = thingAt(e);
    const hit = t && !sameEnd(linkDrag.from, t.end) ? t.g : null;
    if (hit !== linkDrag.hit) {
      linkDrag.hit?.classList.remove("target");
      hit?.classList.add("target");
      linkDrag.hit = hit;
    }
  } else if (state.mode?.connect && state.mode.from) {
    const a = endPos(state.mode.from), g = $("#ghost");
    g.setAttribute("x1", a.x); g.setAttribute("y1", a.y);
    g.setAttribute("x2", p.x); g.setAttribute("y2", p.y);
    g.setAttribute("visibility", "visible");
  }
});

$("#canvas").addEventListener("pointerup", async (e) => {
  if (linkDrag) {
    const { from, hit } = linkDrag;
    linkDrag = null;
    hit?.classList.remove("target");
    $("#ghost").setAttribute("visibility", "hidden");
    const t = thingAt(e);
    if (t && !sameEnd(from, t.end)) await linkDialog(from, t.end);
    return;
  }
  if (!drag) return;
  const { kind, o, moved } = drag;
  drag = null;
  // a plain left click (no drag) opens the device menu at the pointer
  if (!moved && e.button === 0) return showMenu(kind, o);
  if (moved) await api("PATCH", `/labs/${state.lab.id}/${kind === "node" ? "nodes" : "networks"}/${o.id}`, { x: o.x, y: o.y });
});

$("#canvas").addEventListener("pointerdown", async (e) => {
  if (!state.lab) return;
  const p = canvasPoint(e);
  if (state.mode?.place) {
    const m = state.mode.place;
    if (!e.shiftKey) setMode(null);  // shift-click keeps placing
    try { await placeDevice(m, p); } catch {}
    return;
  }
  if (state.mode?.connect) {
    if (state.mode.once) return setMode(null);  // "Connect to…" from the menu: a click on empty space cancels
    state.mode.from = null; $("#ghost").setAttribute("visibility", "hidden"); render(); return;
  }
  select(null);
});

async function connectClick(kind, o) {
  const end = kind === "node" ? { node: o.id } : { network: o.id };
  const from = state.mode.from, once = state.mode.once;
  if (!from) {
    state.mode.from = end; render(); return;
  }
  state.mode.from = null;
  $("#ghost").setAttribute("visibility", "hidden");
  render();
  if (once) setMode(null);
  if (!sameEnd(from, end)) await linkDialog(from, end);
}

// ---------------------------------------------------------------- linking

let linkDrag = null;  // {from, hit}: dragging from a device's connector

const sameEnd = (a, b) => (a.node && a.node === b.node) || (a.network && a.network === b.network);

function startLinkDrag(e, kind, o) {
  e.stopPropagation();
  if (e.button !== 0 || state.mode?.place) return;
  linkDrag = { from: kind === "node" ? { node: o.id } : { network: o.id }, hit: null };
  $("#canvas").setPointerCapture(e.pointerId);
}

function thingAt(e) {
  const g = document.elementFromPoint(e.clientX, e.clientY)?.closest("#nodeLayer .node");
  if (!g) return null;
  return { g, end: g.dataset.kind === "node" ? { node: g.dataset.id } : { network: g.dataset.id } };
}

function thingName(end) {
  return end.node ? state.lab.nodes[end.node]?.name : state.lab.networks[end.network]?.name;
}

// ask which port to use on each device, then create the link
async function linkDialog(a, b) {
  if (a.network && b.network) return toast("Two switches or clouds cannot be linked directly. Connect each to a device.");
  if (a.network) [a, b] = [b, a];  // the API wants the device on side a
  const portSelect = (end) => {
    const n = state.lab.nodes[end.node], used = new Map(linksOf(end.node)), s = el("select");
    let first = null;
    for (let i = 0; i < n.ifaces; i++) {
      const peer = used.get(i);
      s.append(el("option", { value: i, disabled: !!peer,
        text: peer ? `${ifName(end.node, i)}  (in use: ${endName(peer)})` : ifName(end.node, i) }));
      if (!peer && first === null) first = i;
    }
    if (first === null) return null;
    s.value = String(first);
    return s;
  };
  const full = (end) => toast(`${thingName(end)} has no free port. Stop it and raise Interfaces in its settings.`);
  const sa = portSelect(a);
  if (!sa) return full(a);
  const sb = b.node ? portSelect(b) : null;
  if (b.node && !sb) return full(b);
  const row = (label, input) => el("label", { class: "stack" }, label, input);
  const body = el("div", {},
    row(`${thingName(a)} port`, sa),
    b.node ? row(`${thingName(b)} port`, sb)
      : el("p", { text: `${thingName(b)} is a ${state.lab.networks[b.network].type === "bridge" || !state.lab.networks[b.network].type ? "switch" : "cloud"}; it takes any number of links.` }));
  const ok = await modal({ title: `Connect ${thingName(a)} to ${thingName(b)}`, ok: "Connect", body, focus: sa });
  if (!ok) return;
  try {
    await api("POST", `/labs/${state.lab.id}/links`, {
      a: { node: a.node, iface: Number(sa.value) },
      b: b.node ? { node: b.node, iface: Number(sb.value) } : { network: b.network },
    });
  } catch {}
  await reloadLab();
}

// ---------------------------------------------------------------- inspector

function field(label, input) { return el("div", { class: "field" }, el("label", { text: label }), input); }

function numberInput(value, onchange, opts = {}) {
  return el("input", { type: "number", value, min: opts.min, max: opts.max, step: opts.step, disabled: opts.disabled,
    onchange: (e) => onchange(Number(e.target.value)) });
}

function textInput(value, onchange) {
  return el("input", { type: "text", value, onchange: (e) => onchange(e.target.value.trim()) });
}

function renderInspector() {
  const box = $("#inspector");
  box.innerHTML = "";
  const lab = state.lab;
  if (!lab) return;
  const sel = state.sel;
  const obj = sel && (sel.kind === "node" ? lab.nodes : sel.kind === "network" ? lab.networks : lab.links)[sel.id];
  if (!obj) return renderLabInfo(box);
  if (sel.kind === "node") return renderNode(box, obj);
  if (sel.kind === "network") return renderNetwork(box, obj);
  renderLink(box, obj);
}

function renderLabInfo(box) {
  const lab = state.lab;
  box.append(
    el("h2", { text: "Lab" }),
    el("p", { class: "big", text: lab.name }),
    el("p", { class: "sub", text: `${Object.keys(lab.nodes).length} nodes · ${Object.keys(lab.networks).length} networks · ${Object.keys(lab.links).length} links` }),
    el("h2", { text: "Controls" }),
    el("ul", { class: "help" },
      el("li", { text: "Drag a device from the Device list onto the lab sheet. Or click it, then click the sheet; hold Shift to place several." }),
      el("li", { text: "Connect: drag the orange plug on a device onto another device, then pick the port on each side. Or press L and click two devices." }),
      el("li", { text: "Click a device (or right-click it) for its menu: start, stop, console, connect, rename, wipe, delete. Double-click a running node to open its console." }),
      el("li", { text: "Delete removes the selection. Esc cancels the current tool." }),
      el("li", { text: "Zoom: Ctrl + scroll or pinch, the buttons at the bottom right, or + and -. 0 resets to 100%, F fits the whole topology." }),
      el("li", { text: "Router configs persist when you run write memory in vtysh." })),
  );
}

function linksOf(nodeId) {
  return Object.values(state.lab.links).flatMap((l) =>
    l.a.node === nodeId ? [[l.a.iface, l.b]] : l.b.node === nodeId ? [[l.b.iface, l.a]] : []);
}

// node actions shared by the inspector and the click menu: [label, fn, class]
function nodeActions(n) {
  const tpl = state.tpl.templates[n.template];
  const running = state.status[n.id] === "running";
  const path = `/labs/${state.lab.id}/nodes/${n.id}`;
  const run = (a) => async () => { await api("POST", `${path}/${a}`); await reloadLab(); };
  const out = [running ? ["Stop", run("stop")] : ["Start", run("start"), "accent"]];
  if (running) for (const c of tpl.consoles) out.push([`Console: ${c}`, () => openConsole(n, c)]);
  if (n.template !== "qemu") out.push(["Startup config", () => editConfig(n)]);
  out.push(["Wipe", async () => {
    const ok = await modal({ title: `Wipe ${n.name}?`, ok: "Wipe",
      body: el("p", { text: "Stops the node and deletes its saved config and disk changes. It boots with its initial config next time." }) });
    if (ok) await run("wipe")();
  }]);
  out.push(["Delete", () => { select("node", n.id); deleteSelection(); }, "danger"]);
  return out;
}

function renderNode(box, n) {
  const lab = state.lab, tpl = state.tpl.templates[n.template];
  const running = state.status[n.id] === "running";
  const path = `/labs/${lab.id}/nodes/${n.id}`;
  const patch = async (f) => { try { await api("PATCH", path, f); } finally { await reloadLab(); } };

  box.append(
    el("h2", { text: `Node ${String(n.id).padStart(2, "0")}` }),
    el("p", { class: "big", text: n.name }),
    el("p", { class: "sub" }, tpl.label + (n.image ? ` · ${n.image}` : "") + " · ",
      el("span", { class: "state" + (running ? " running" : ""), text: running ? "running" : "stopped" })),
  );
  const acts = el("div", { class: "actions" });
  for (const [label, fn, cls] of nodeActions(n)) acts.append(el("button", { text: label, class: cls, onclick: fn }));
  box.append(acts);

  box.append(el("h2", { text: "Settings" }),
    field("Name", textInput(n.name, (v) => v && patch({ name: v }))),
    field("Interfaces", numberInput(n.ifaces, (v) => patch({ ifaces: v }), { min: 1, max: 16, disabled: running })),
    field("RAM (MB)", numberInput(n.ram, (v) => patch({ ram: v }), { min: 32, step: 32, disabled: running })));
  if (n.template === "qemu") box.append(field("vCPU", numberInput(n.cpu, (v) => patch({ cpu: v }), { min: 1, max: 8, disabled: running })));
  if (running) box.append(el("p", { class: "note", text: "Stop the node to change interfaces or memory." }));

  const used = new Map(linksOf(n.id));
  const t = el("table");
  for (let i = 0; i < n.ifaces; i++) {
    const peer = used.get(i);
    t.append(el("tr", {}, el("td", { text: ifName(n.id, i) }), el("td", { class: peer ? "" : "none", text: peer ? endName(peer) : "not connected" })));
  }
  box.append(el("h2", { text: "Interfaces" }), t);
}

function renderNetwork(box, net) {
  const lab = state.lab;
  const ports = Object.values(lab.links).filter((l) => l.b.network === net.id);
  const nat = state.tpl.clouds.nat, type = net.type || "bridge";
  const host = state.tpl.clouds.host_bridges.find((b) => b.bridge === net.bridge);
  const about = {
    bridge: ["Switch", "Layer-2 segment (Linux bridge). Every connected port shares one broadcast domain."],
    nat: ["NAT Cloud", `Shared by all labs. Subnet ${nat.subnet}, gateway ${nat.gateway}` +
      (nat.dhcp ? `, DHCP ${nat.dhcp}.` : ", no DHCP: set addresses by hand.") +
      " Traffic leaves through the server's address, so nodes reach the server LAN and the internet; the LAN cannot reach the nodes."],
    pnet: ["Cloud", `Bridged at layer 2 to the server's network through host bridge ${net.bridge}` +
      (host?.members.length ? ` (${host.members.join(", ")})` : "") +
      ". Nodes sit directly on that LAN: use its addresses or its DHCP server."],
  }[type];
  box.append(
    el("h2", { text: about[0] }),
    el("p", { class: "big", text: net.name }),
    el("p", { class: "sub", text: about[1] }),
    field("Name", textInput(net.name, async (v) => { if (v) { await api("PATCH", `/labs/${lab.id}/networks/${net.id}`, { name: v }); await reloadLab(); } })),
    el("div", { class: "actions" }, el("button", { text: "Delete", class: "danger", onclick: () => deleteSelection() })),
    el("h2", { text: "Ports" }),
  );
  const t = el("table");
  if (!ports.length) t.append(el("tr", {}, el("td", { class: "none", colspan: 2, text: "Nothing connected" })));
  ports.forEach((l, i) => t.append(el("tr", {}, el("td", { text: `p${i + 1}` }), el("td", { text: endName(l.a) }))));
  box.append(t);
}

function renderLink(box, l) {
  box.append(
    el("h2", { text: "Link" }),
    el("p", { class: "big", text: endName(l.a) }),
    el("p", { class: "sub" }, "to " + endName(l.b) + " · ",
      el("span", { class: "state " + (state.linkStatus[l.id] === "up" ? "up" : "down"), text: state.linkStatus[l.id] === "up" ? "link up" : "link down" })),
    el("p", { class: "note", text: "Links can be added and removed while nodes are running." }),
    el("div", { class: "actions" }, el("button", { text: "Delete link", class: "danger", onclick: () => deleteSelection() })),
  );
}

async function editConfig(n) {
  const path = `/labs/${state.lab.id}/nodes/${n.id}/config`;
  const { text } = await api("GET", path);
  const ta = el("textarea", { spellcheck: "false" });
  ta.value = text;
  const note = n.template === "frr"
    ? "frr.conf. Applied when the router starts. On a running router, write memory in vtysh saves here."
    : "startup.sh. Runs as a shell script each time the PC starts.";
  const ok = await modal({ title: `${n.name} startup config`, ok: "Save", body: el("div", {}, el("p", { text: note }), ta) });
  if (ok) { await api("PUT", path, { text: ta.value }); toast("Saved. Restart the node to apply."); }
}

async function deleteSelection() {
  const s = state.sel, lab = state.lab;
  if (!s || !lab) return;
  if (s.kind === "link") await api("DELETE", `/labs/${lab.id}/links/${s.id}`);
  else {
    const o = (s.kind === "node" ? lab.nodes : lab.networks)[s.id];
    const ok = await modal({ title: `Delete ${o.name}?`, ok: "Delete",
      body: el("p", { text: s.kind === "node" ? "The node is stopped and its links and saved config are removed." : "Its links are removed." }) });
    if (!ok) return;
    if (s.kind === "node") closeConsoles((c) => c.lab === lab.id && c.node === s.id);
    await api("DELETE", `/labs/${lab.id}/${s.kind === "node" ? "nodes" : "networks"}/${s.id}`);
  }
  state.sel = null;
  await reloadLab();
}

document.addEventListener("keydown", (e) => {
  if (e.target.closest("input,textarea,select") || !$("#modal").hidden || e.target.closest(".xterm")) return;
  if (e.key === "Escape") { closeMenu(); setMode(null); select(null); }
  else if (e.key === "Delete" || e.key === "Backspace") deleteSelection();
  else if (e.key.toLowerCase() === "l") actions.toggleConnect();
  else if (e.ctrlKey || e.metaKey || e.altKey) return;
  else if (e.key === "+" || e.key === "=") zoomStep(1);
  else if (e.key === "-" || e.key === "_") zoomStep(-1);
  else if (e.key === "0") setZoom(1);
  else if (e.key.toLowerCase() === "f") zoomToFit();
});

// ---------------------------------------------------------------- consoles

function openConsole(n, kind, focus = true) {
  if (state.status[n.id] !== "running") return toast(`${n.name} is not running. Start it first.`);
  const tpl = state.tpl.templates[n.template];
  kind = kind || tpl.consoles[0];
  // some devices (Huawei VRP, ...) erase with Ctrl+H instead of DEL; remembered per image in this browser
  const bsKey = "netlab.backspace." + (n.image || n.template);
  const bsDefault = state.tpl.qemu_images.find((m) => m.name === n.image)?.backspace || "del";
  openTerm({ key: `node/${state.lab.id}/${n.id}/${kind}`, lab: state.lab.id, node: n.id, kind, title: n.name,
    url: `/ws/console/${state.lab.id}/${n.id}?kind=${kind}`, bsKey, bs: store.get(bsKey) || bsDefault }, focus);
}

// a browser terminal on a server WebSocket, mounted in parent: node consoles and AI assistant sessions
function makeTerm(spec, parent, onclose) {
  const box = el("div", { class: "term" });
  parent.append(box);
  const term = new Terminal({
    fontFamily: 'ui-monospace, "SF Mono", Menlo, Consolas, monospace', fontSize: 13, cursorBlink: true,
    theme: { background: "#FFFFFF", foreground: "#111111", cursor: "#FF4F00", selectionBackground: "#FFD3BF" },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(box);
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const sep = spec.url?.includes("?") ? "&" : "?";
  // no url: a read-only log (the assistant's commands on a container node)
  const ws = spec.url ? new WebSocket(`${proto}://${location.host}${spec.url}${sep}token=${encodeURIComponent(state.token)}`)
    : { readyState: 1, send() {}, close() { this.readyState = 3; } };
  ws.binaryType = "arraybuffer";
  const c = { id: crypto.randomUUID?.() || String(Math.random()), ...spec, term, fit, ws, box };
  const resize = () => ws.readyState === 1 && ws.send(JSON.stringify({ t: "r", c: term.cols, r: term.rows }));
  ws.onopen = () => { fit.fit(); resize(); };
  ws.onmessage = (e) => term.write(new Uint8Array(e.data));
  ws.onclose = (e) => {
    term.write(e.code === 4401 ? "\r\n\x1b[2m[session expired: sign in again, then reopen the console]\x1b[0m\r\n"
      : "\r\n\x1b[2m[console closed]\x1b[0m\r\n");
    onclose?.();
  };
  term.onData((d) => {
    if (c.bs === "ctrl-h") d = d.replace(/\x7f/g, "\x08");
    if (ws.readyState === 1) ws.send(JSON.stringify({ t: "i", d }));
  });
  term.onResize(resize);
  return c;
}

function openTerm(spec, focus = true) {
  const existing = state.consoles.find((c) => c.key === spec.key && c.ws.readyState <= 1);
  if (existing) { showConsole(existing, focus); return existing; }
  $("#dock").hidden = false;
  const c = makeTerm(spec, $("#terms"), renderTabs);
  state.consoles.push(c);
  showConsole(c, focus);
  return c;
}

// ---------------------------------------------------------------- following the AI assistant

// node id -> {seq, log} last shown; seeded when a lab opens, so earlier activity does not pop tabs
const aiSeen = {};

function aiSeed(activity) {
  for (const k of Object.keys(aiSeen)) delete aiSeen[k];
  for (const [id, r] of Object.entries(activity || {})) aiSeen[id] = { seq: r.seq, log: r.log || "" };
}

// open the console of each node the assistant reaches (one at a time), without taking the keyboard
function followAI(activity) {
  for (const [id, r] of Object.entries(activity || {})) {
    const n = state.lab.nodes[id], seen = aiSeen[id];
    if (!n || (seen && seen.seq === r.seq && seen.log === (r.log || ""))) continue;
    aiSeen[id] = { seq: r.seq, log: r.log || "" };
    if (state.tpl.templates[n.template].kind === "qemu") {
      if (!seen || seen.seq !== r.seq) openConsole(n, undefined, false);
      continue;
    }
    // containers run the assistant's commands with exec: show them in an "AI" log tab
    const c = openTerm({ key: `ailog/${state.lab.id}/${id}`, lab: state.lab.id, node: id, kind: "AI commands",
      title: n.name }, false);
    const log = r.log || "";
    c.term.write(seen && seen.seq === r.seq && log.startsWith(seen.log) ? log.slice(seen.log.length) : "\r\n" + log);
  }
  const active = new Set(Object.entries(activity || {}).filter(([, r]) => r.active).map(([id]) => id));
  if (String([...active]) !== String([...state.aiActive])) { state.aiActive = active; renderTabs(); render(); }
}

function showConsole(c, focus = true) {
  state.active = c.id;
  for (const x of state.consoles) x.box.style.visibility = x === c ? "visible" : "hidden";
  renderTabs();
  requestAnimationFrame(() => { c.fit.fit(); if (focus) c.term.focus(); });
}

function closeConsole(c) {
  c.ws.close(); c.term.dispose(); c.box.remove();
  state.consoles = state.consoles.filter((x) => x !== c);
  if (state.active === c.id) {
    const next = state.consoles[state.consoles.length - 1];
    if (next) showConsole(next);
  }
  if (!state.consoles.length) $("#dock").hidden = true;
  renderTabs();
}

function closeConsoles(pred) { state.consoles.filter(pred).forEach(closeConsole); }

function renderTabs() {
  const tabs = $("#tabs");
  tabs.innerHTML = "";
  for (const c of state.consoles) {
    const closeBtn = el("button", { "aria-label": `Close ${c.title} console`, text: "Close",
      onclick: (e) => { e.stopPropagation(); closeConsole(c); } });
    const bsBtn = c.bsKey && el("button", { text: c.bs === "ctrl-h" ? "Backspace: Ctrl+H" : "Backspace: DEL",
      title: "What the Backspace key sends. Switch it if Backspace does not erase on this device.",
      onclick: (e) => { e.stopPropagation(); c.bs = c.bs === "ctrl-h" ? "del" : "ctrl-h"; store.set(c.bsKey, c.bs); renderTabs(); c.term.focus(); } });
    tabs.append(el("div", { class: "tab" + (c.id === state.active ? " on" : ""), onclick: () => showConsole(c) },
      el("b", { text: c.title }), el("span", { text: c.ws.readyState > 1 ? `${c.kind}, closed` : c.kind + (c.node && state.aiActive.has(c.node) && c.lab === state.lab?.id ? " · AI working" : "") }), ...(bsBtn ? [bsBtn] : []), closeBtn));
  }
}

// drag the top edge of the dock to resize it
$("#dockGrip").addEventListener("pointerdown", (e) => {
  const grip = e.target; grip.setPointerCapture(e.pointerId);
  const move = (ev) => {
    const h = Math.max(140, Math.min(window.innerHeight - 160, window.innerHeight - ev.clientY));
    document.documentElement.style.setProperty("--dock-h", h + "px");
    state.consoles.forEach((c) => c.fit.fit());
  };
  grip.addEventListener("pointermove", move);
  grip.addEventListener("pointerup", () => grip.removeEventListener("pointermove", move), { once: true });
});
window.addEventListener("resize", () => [...state.consoles, ...ai.sessions].forEach((c) => c.fit.fit()));

// ---------------------------------------------------------------- boot

async function poll() {
  if (state.lab && !drag && document.visibilityState === "visible") {
    try {
      const s = await api("GET", `/labs/${state.lab.id}/status`);
      followAI(s.ai);
      if (JSON.stringify(s.nodes) !== JSON.stringify(state.status) || JSON.stringify(s.links) !== JSON.stringify(state.linkStatus)) {
        state.status = s.nodes;
        absorbLinkStatus(s.links);
        render();
      }
    } catch {}
  }
  // follow the assistant closely while one is running
  setTimeout(poll, ai.sessions.some((c) => c.mode === "chat" && c.ws.readyState <= 1) ? 1000 : 3000);
}

(async function boot() {
  if (!state.token) await login();
  else {
    const me = await api("GET", "/me");  // may pass through the sign-in screen, which renews state.token
    setSession({ token: state.token, ...me });
  }
  state.tpl = await api("GET", "/templates");
  renderPalette();
  await loadLabs();
  if (ai.open) aiLoadProviders();
  renderAI();
  checkUpdate();
  setInterval(checkUpdate, 6 * 3600 * 1000);
  poll();
})();
