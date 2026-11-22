// SafeFace web app: everything runs in the browser.
//   camera (getUserMedia) -> motion gate -> face-api detect + describe -> match -> log event
// Data (people, faces, events) lives in Firebase Firestore when hosted on Firebase,
// otherwise in this browser's storage.
import { loadModels, detectLive, detectPhoto, matchFace, cropToDataUrl, packDescriptor } from "./faces.js";
import { FirebaseStore, LocalStore, getFirebaseConfig } from "./store.js";

// ============================================================================ helpers
const $ = (s) => document.querySelector(s);
const el = (tag, props = {}, ...children) => {
  const n = Object.assign(document.createElement(tag), props);
  for (const c of children) if (c != null && c !== false) n.append(c);
  return n;
};
const pct = (c) => `${Math.round((c || 0) * 100)}%`;
const fmtTime = (ts) => new Date(ts).toLocaleString();
const fmtAgo = (ts) => {
  const s = Math.round((Date.now() - ts) / 1000);
  if (s < 60) return `${Math.max(0, s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(ts).toLocaleDateString();
};
const LS = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage blocked */ } },
};
const msgIn = (sel, text, ok) => $(sel).replaceChildren(...(text ? [el("div", { className: ok ? "ok" : "err", textContent: text })] : []));

function toast(text, { img, kind = "" } = {}) {
  const t = el("div", { className: `toast ${kind}` }, img ? el("img", { src: img, alt: "" }) : null, el("div", { textContent: text }));
  $("#toasts").append(t);
  setTimeout(() => t.remove(), 5000);
}

let audioCtx;
function beep() {
  if (!$("#soundToggle").checked) return;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.frequency.value = 880; g.gain.value = 0.08;
    o.connect(g).connect(audioCtx.destination);
    o.start(); o.stop(audioCtx.currentTime + 0.15);
  } catch { /* audio needs a user click first */ }
}

function banner(html, kind = "") {
  const b = $("#banner");
  if (!html) { b.classList.add("hidden"); return; }
  b.className = `banner ${kind}`;
  b.replaceChildren(...(Array.isArray(html) ? html : [html]));
}

function setPill(text, kind) { const p = $("#statusPill"); p.className = `pill pill-${kind}`; p.textContent = text; }

// ============================================================================ settings
const DEFAULTS = { threshold: 0.5, fps: 3, cooldown: 30, motion: true, unknown: false, camName: "", cameraId: "", autoStart: false };
const settings = { ...DEFAULTS, ...LS.get("safeface.settings", {}) };
const saveSettings = () => LS.set("safeface.settings", settings);
const strictnessWord = (t) => (t <= 0.43 ? "very strict" : t <= 0.48 ? "strict" : t <= 0.53 ? "normal" : "relaxed");

// ============================================================================ state
const state = {
  store: null, user: null, unsubs: [],
  people: [], faces: [], events: [],
  seen: new Set(), eventsLoaded: false,
  modelsReady: false, backend: "",
};

// ============================================================================ boot
async function main() {
  splash();
  setPill("Loading…", "muted");
  const modelsPromise = loadModels((t) => { $("#feedIdleText").textContent = t; setPill("Loading AI…", "muted"); })
    .then((backend) => {
      state.modelsReady = true; state.backend = backend;
      $("#startBtn").disabled = false;
      $("#feedIdleText").textContent = "Recognition runs right here in your browser. Video never leaves this device.";
      if (backend !== "webgl") toast("Graphics acceleration is unavailable, so recognition will be slow on this device.", { kind: "error" });
      setPill("Camera off", "warn");
    })
    .catch((e) => {
      console.error(e);
      setPill("AI failed to load", "off");
      $("#feedIdleTitle").textContent = "Couldn't load the face recognition models";
      $("#feedIdleText").textContent = "Check your internet connection and reload the page.";
    });

  const cfg = await getFirebaseConfig();
  if (cfg && cfg.apiKey) {
    try {
      state.store = await FirebaseStore.create(cfg);
    } catch (e) {
      console.error(e);
      banner(`Couldn't start Firebase: ${e.message}`, "error");
      return;
    }
    $("#signinBtn").addEventListener("click", async () => {
      msgIn("#signinMsg", "");
      try { await state.store.signIn(); }
      catch (e) { if (e.code !== "auth/popup-closed-by-user") msgIn("#signinMsg", signInError(e), false); }
    });
    state.store.onAuth((user) => (user ? onSignedIn(user, modelsPromise) : onSignedOut()));
  } else {
    if (cfg && !cfg.apiKey) {
      banner("This Firebase project has no Web app registered yet, so data is only saved on this device. " +
        "In the Firebase console open Project settings → Your apps → Add app → Web, then reload.");
    }
    state.store = new LocalStore();
    $("#modeBadge").classList.remove("hidden");
    subscribe();
    modelsPromise.then(maybeAutoStart);
  }
}

function signInError(e) {
  if (e.code === "auth/operation-not-allowed" || e.code === "auth/configuration-not-found")
    return "Google sign-in isn't enabled for this project yet. In the Firebase console go to Authentication → Sign-in method and enable Google.";
  if (e.code === "auth/unauthorized-domain") return `This website address isn't authorised for sign-in. Add ${location.hostname} under Authentication → Settings → Authorized domains.`;
  if (e.code === "auth/network-request-failed") return "No internet connection.";
  return e.message || String(e);
}

function onSignedIn(user, modelsPromise) {
  state.user = user;
  $("#signin").classList.add("hidden");
  $("#userEmail").textContent = user.email || user.displayName || "";
  $("#userChip").classList.remove("hidden");
  subscribe();
  modelsPromise.then(maybeAutoStart);
}

function onSignedOut() {
  state.user = null;
  stopCamera();
  state.unsubs.forEach((u) => u()); state.unsubs = [];
  state.people = []; state.faces = []; state.events = []; state.seen.clear(); state.eventsLoaded = false;
  renderPeople(); renderEvents([]);
  banner("");
  $("#userChip").classList.add("hidden");
  $("#signin").classList.remove("hidden");
}

$("#signoutBtn").addEventListener("click", () => state.store?.signOut?.());

function onDbError(err) {
  console.error(err);
  if (err.code === "permission-denied") {
    const who = state.user?.email || "This account";
    banner([el("strong", { textContent: "Access denied. " }),
      `${who} isn't allowed to use this SafeFace. Ask the administrator to add it to firestore.rules and redeploy. `,
      el("button", { className: "link-btn", textContent: "Sign in with another account", onclick: () => state.store.signOut() })], "error");
  } else {
    banner(`Couldn't load data from Firebase (${err.code || err.message}). If this is a new project, create the Firestore database first (Firebase console → Firestore Database → Create database).`, "error");
  }
}

function subscribe() {
  const s = state.store;
  state.unsubs.forEach((u) => u());
  state.unsubs = [
    s.onPeople((list) => { state.people = list; renderPeople(); }, onDbError),
    s.onFaces((list) => { state.faces = list; updateStats(); }, onDbError),
    s.onEvents(onEvents, onDbError),
  ];
}

// ============================================================================ camera
const video = $("#video"), overlay = $("#overlay"), octx = overlay.getContext("2d");
const cam = { stream: null, running: false, timer: 0, raf: 0, wakeLock: null, label: "" };
const live = { faces: [], at: 0, ms: 0, passes: 0, fps: 0, statT: performance.now(), lastActivity: 0, active: false, lastEvent: new Map() };

async function maybeAutoStart() {
  if (!settings.autoStart || cam.running) return;
  try {
    const p = await navigator.permissions.query({ name: "camera" });
    if (p.state === "granted") startCamera();
  } catch { /* permissions API unsupported: wait for a click */ }
}

async function populateCameras() {
  try {
    const devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
    const sel = $("#cameraSelect");
    sel.replaceChildren(el("option", { value: "", textContent: "Default camera" }),
      ...devices.map((d, i) => el("option", { value: d.deviceId, textContent: d.label || `Camera ${i + 1}` })));
    sel.value = devices.some((d) => d.deviceId === settings.cameraId) ? settings.cameraId : "";
  } catch { /* ignore */ }
}

async function startCamera() {
  if (!state.modelsReady) return;
  if (!navigator.mediaDevices?.getUserMedia) {
    return cameraError("This browser can't access cameras. Use an up-to-date Chrome, Edge, Firefox or Safari over https.");
  }
  stopCamera(false);
  setPill("Starting camera…", "muted");
  const video_ = settings.cameraId
    ? { deviceId: { exact: settings.cameraId }, width: { ideal: 1280 }, height: { ideal: 720 } }
    : { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } };
  try {
    cam.stream = await navigator.mediaDevices.getUserMedia({ video: video_, audio: false });
  } catch (e) {
    if (e.name === "OverconstrainedError" && settings.cameraId) { settings.cameraId = ""; saveSettings(); return startCamera(); }
    const why = {
      NotAllowedError: "Camera permission was blocked. Click the camera icon in the address bar, allow the camera, then press Start.",
      NotFoundError: "No camera was found on this device.",
      NotReadableError: "The camera is being used by another app (Zoom, Teams…). Close it and press Start.",
    }[e.name] || `Couldn't start the camera: ${e.message}`;
    return cameraError(why);
  }
  const track = cam.stream.getVideoTracks()[0];
  cam.label = track.label || "Camera";
  track.addEventListener("ended", () => { stopCamera(); cameraError("The camera was disconnected."); });
  video.srcObject = cam.stream;
  await video.play().catch(() => {});
  overlay.width = video.videoWidth || 1280; overlay.height = video.videoHeight || 720;
  $("#feedIdle").classList.add("hidden");
  $("#stopBtn").classList.remove("hidden");
  cam.running = true;
  settings.autoStart = true; saveSettings();
  populateCameras();
  requestWakeLock();
  live.lastActivity = performance.now(); live.active = true;
  updateStats();
  tick();
  draw();
}

function cameraError(text) {
  setPill("Camera error", "off");
  $("#feedIdleTitle").textContent = "Camera not available";
  $("#feedIdleText").textContent = text;
  $("#feedIdle").classList.remove("hidden");
}

function stopCamera(userAction = false) {
  cam.running = false;
  clearTimeout(cam.timer); cancelAnimationFrame(cam.raf);
  cam.stream?.getTracks().forEach((t) => t.stop());
  cam.stream = null; video.srcObject = null;
  octx.clearRect(0, 0, overlay.width, overlay.height);
  cam.wakeLock?.release?.().catch(() => {}); cam.wakeLock = null;
  $("#stopBtn").classList.add("hidden");
  if (userAction) { settings.autoStart = false; saveSettings(); }
  if (state.modelsReady) {
    $("#feedIdleTitle").textContent = "Camera is off";
    $("#feedIdleText").textContent = "Recognition runs right here in your browser. Video never leaves this device.";
    $("#feedIdle").classList.remove("hidden");
    setPill("Camera off", "warn");
  }
}

async function requestWakeLock() {
  try { cam.wakeLock = await navigator.wakeLock?.request("screen"); } catch { /* not supported / denied */ }
}
document.addEventListener("visibilitychange", () => { if (!document.hidden && cam.running) requestWakeLock(); });

$("#startBtn").addEventListener("click", startCamera);
$("#stopBtn").addEventListener("click", () => stopCamera(true));
$("#cameraSelect").addEventListener("change", (e) => { settings.cameraId = e.target.value; saveSettings(); if (cam.running) startCamera(); });

// ---- motion gate: compare tiny greyscale frames ----------------------------
const mCanvas = document.createElement("canvas"), mCtx = mCanvas.getContext("2d", { willReadFrequently: true });
let mPrev = null;
function motionDetected() {
  const w = 160, h = Math.max(1, Math.round(160 * video.videoHeight / video.videoWidth));
  if (mCanvas.width !== w || mCanvas.height !== h) { mCanvas.width = w; mCanvas.height = h; mPrev = null; }
  mCtx.drawImage(video, 0, 0, w, h);
  const px = mCtx.getImageData(0, 0, w, h).data;
  const grey = new Float32Array(w * h);
  for (let i = 0, j = 0; j < grey.length; i += 4, j++) grey[j] = px[i] * 0.299 + px[i + 1] * 0.587 + px[i + 2] * 0.114;
  if (!mPrev) { mPrev = grey; return true; }
  let changed = 0;
  for (let j = 0; j < grey.length; j++) {
    if (Math.abs(grey[j] - mPrev[j]) > 25) changed++;
    mPrev[j] = mPrev[j] * 0.75 + grey[j] * 0.25;
  }
  return changed / grey.length >= 0.004;
}

// ---- frame grabber: detect and crop on the SAME frozen frame -----------------
// (on slower devices detection takes a while; cropping from the live video would
//  grab a later frame where the person has already moved)
function grabFrame() {
  const c = document.createElement("canvas");
  c.width = video.videoWidth; c.height = video.videoHeight;
  c.getContext("2d").drawImage(video, 0, 0);
  return c;
}

// ---- recognition loop -------------------------------------------------------
async function tick() {
  if (!cam.running) return;
  const t0 = performance.now();
  try {
    if (video.readyState >= 2 && video.videoWidth) {
      if (overlay.width !== video.videoWidth) { overlay.width = video.videoWidth; overlay.height = video.videoHeight; }
      const motion = settings.motion ? motionDetected() : true;
      if (motion) live.lastActivity = t0;
      live.active = motion || t0 - live.lastActivity < 3000;
      if (live.active) {
        const frame = grabFrame();
        const faces = await detectLive(frame);
        for (const f of faces) f.match = matchFace(f.descriptor, state.faces, settings.threshold);
        live.ms = performance.now() - t0;
        live.passes++;
        if (faces.length) { live.faces = faces; live.at = performance.now(); live.lastActivity = live.at; handleDetections(faces, frame); }
      }
    }
  } catch (e) { console.error("recognition error", e); }
  const now = performance.now();
  if (now - live.statT >= 2000) { live.fps = live.passes / ((now - live.statT) / 1000); live.passes = 0; live.statT = now; updateStats(); }
  cam.timer = setTimeout(tick, Math.max(0, 1000 / settings.fps - (performance.now() - t0)));
}

function updateStats() {
  const n = new Set(state.faces.map((f) => f.personId)).size;
  const enrolled = `${n} ${n === 1 ? "person" : "people"} enrolled`;
  $("#camStats").textContent = cam.running
    ? `${cam.label} · ${live.fps.toFixed(1)} checks/s · ${Math.round(live.ms)} ms · ${enrolled}`
    : enrolled;
  if (cam.running) setPill(live.active ? "Watching · scanning" : "Watching · idle", "ok");
}

function draw() {
  if (!cam.running) return;
  octx.clearRect(0, 0, overlay.width, overlay.height);
  if (performance.now() - live.at < 1500) {
    const s = Math.max(overlay.width / 1280, 0.5);
    octx.font = `600 ${Math.round(16 * s)}px system-ui, sans-serif`;
    octx.lineWidth = Math.max(2, 3 * s);
    for (const f of live.faces) {
      const known = !!f.match?.personId;
      const color = known ? "#22c55e" : "#ef4444";
      const { x, y, width, height } = f.box;
      octx.strokeStyle = color; octx.strokeRect(x, y, width, height);
      const label = known ? `${f.match.name} ${pct(f.match.confidence)}` : "Unknown";
      const tw = octx.measureText(label).width + 12, th = Math.round(22 * s);
      const ly = Math.max(th, y);
      octx.fillStyle = color; octx.fillRect(x - octx.lineWidth / 2, ly - th, tw, th);
      octx.fillStyle = "#fff"; octx.fillText(label, x + 6 - octx.lineWidth / 2, ly - 6 * s);
    }
  }
  octx.font = `600 ${Math.round(14 * Math.max(overlay.width / 1280, 0.6))}px system-ui, sans-serif`;
  octx.fillStyle = "rgba(255,255,255,.9)";
  octx.fillText(new Date().toLocaleString(), 12, overlay.height - 14);
  cam.raf = requestAnimationFrame(draw);
}

function handleDetections(faces, frame) {
  const now = Date.now();
  for (const f of faces) {
    const known = !!f.match.personId;
    if (!known && !settings.unknown) continue;
    const key = known ? f.match.personId : "unknown";
    if (now - (live.lastEvent.get(key) || 0) < settings.cooldown * 1000) continue;
    live.lastEvent.set(key, now);
    state.store.addEvent({
      personId: f.match.personId,
      name: f.match.name,
      confidence: Math.round(f.match.confidence * 1e4) / 1e4,
      snapshot: cropToDataUrl(frame, f.box),
      descriptor: packDescriptor(f.descriptor),
      camera: settings.camName || cam.label,
    }).catch((e) => { console.error(e); toast(`Couldn't save the detection: ${e.code || e.message}`, { kind: "error" }); });
  }
}

// ============================================================================ events: alerts + history
function onEvents(list) {
  state.events = list;
  const fresh = [];
  for (const e of list) if (!state.seen.has(e.id)) { state.seen.add(e.id); if (state.eventsLoaded) fresh.push(e); }
  state.eventsLoaded = true;
  renderEvents(list, new Set(fresh.map((e) => e.id)));
  fresh.reverse().forEach(notify);
}

function notify(ev) {
  beep();
  toast(`${ev.name} detected (${pct(ev.confidence)})`, { img: ev.snapshot, kind: ev.name === "Unknown" ? "unknown" : "" });
  if ("Notification" in window && Notification.permission === "granted" && document.hidden) {
    try { new Notification(`SafeFace: ${ev.name}`, { body: `${pct(ev.confidence)} · ${ev.camera || ""} · ${fmtTime(ev.timestamp)}`, icon: ev.snapshot }); } catch { /* mobile needs a service worker */ }
  }
}

function addFaceButton(ev, label) {
  if (!ev.descriptor || !ev.snapshot) return null;
  return el("button", { type: "button", className: "add-face-btn", textContent: label, title: "Add this face to the database",
    onclick: (e) => { e.stopPropagation(); openNameDialog(ev); } });
}

function renderEvents(list, freshIds = new Set()) {
  const alerts = list.slice(0, 15).map((ev) => {
    const li = el("li", { className: (ev.name === "Unknown" ? "unknown " : "") + (freshIds.has(ev.id) ? "new" : "") },
      el("img", { src: ev.snapshot || "", alt: ev.name }),
      el("div", {}, el("div", { className: "name", textContent: ev.name }),
        el("div", { className: "small muted ago", textContent: `${pct(ev.confidence)} · ${fmtAgo(ev.timestamp)}` })),
      addFaceButton(ev, "+ Add"));
    li.dataset.ts = ev.timestamp; li.dataset.conf = ev.confidence;
    return li;
  });
  $("#alerts").replaceChildren(...(alerts.length ? alerts : [el("li", { className: "empty muted", textContent: "No detections yet." })]));
  renderHistory();
}
setInterval(() => {
  for (const li of document.querySelectorAll("#alerts li[data-ts]")) li.querySelector(".ago").textContent = `${pct(+li.dataset.conf)} · ${fmtAgo(+li.dataset.ts)}`;
}, 15000);

function renderHistory() {
  const f = $("#historyFilter").value.trim().toLowerCase();
  const rows = state.events.filter((e) => !f || e.name.toLowerCase().includes(f)).map((ev) => el("tr", {},
    el("td", {}, el("img", { src: ev.snapshot || "", alt: "", loading: "lazy" })),
    el("td", { textContent: ev.name }),
    el("td", { className: "conf", textContent: pct(ev.confidence) }),
    el("td", { textContent: fmtTime(ev.timestamp) }),
    el("td", { className: "muted", textContent: ev.camera || "" }),
    el("td", {}, addFaceButton(ev, "+ Add face"))));
  $("#historyBody").replaceChildren(...(rows.length ? rows : [el("tr", {}, el("td", { colSpan: 6, className: "muted", textContent: "No events." }))]));
}
$("#historyFilter").addEventListener("input", renderHistory);
$("#clearHistory").addEventListener("click", async () => {
  if (!confirm("Delete ALL detection events?")) return;
  try { await state.store.clearEvents(); } catch (e) { toast(`Couldn't clear: ${e.code || e.message}`, { kind: "error" }); }
});

// ============================================================================ people
function renderPeople() {
  $("#peopleCount").textContent = `(${state.people.length})`;
  $("#peopleNames").replaceChildren(...state.people.map((p) => el("option", { value: p.name })));
  const items = state.people.map((p) => el("li", {},
    el("img", { src: p.photo || "", alt: "" }),
    el("div", { className: "info" },
      el("div", { textContent: p.name }),
      el("div", { className: "small muted", textContent: `${p.faceCount || 0} photo(s)` + (p.lastSeen ? ` · seen ${fmtAgo(p.lastSeen)}` : "") })),
    el("button", { title: `Remove ${p.name}`, textContent: "✕", onclick: async () => {
      if (!confirm(`Remove ${p.name} and all their reference photos?`)) return;
      try { await state.store.deletePerson(p.id); } catch (e) { toast(`Couldn't remove: ${e.code || e.message}`, { kind: "error" }); }
    } })));
  $("#peopleList").replaceChildren(...(items.length ? items : [el("li", { className: "muted", textContent: "Nobody enrolled yet." })]));
  updateStats();
}

function cleanName(n) {
  n = (n || "").trim().replace(/\s+/g, " ");
  if (!n || n.length > 64) throw new Error("Names must be 1–64 characters.");
  if (n.toLowerCase() === "unknown") throw new Error('"Unknown" is reserved for unrecognised faces.');
  return n;
}

// ---- add faces from a photo -------------------------------------------------
const photo = { faces: [], canvas: null };
const photoFile = $("#photoFile"), photoDrop = $("#photoDrop");

function resetPhoto() {
  photo.faces = []; photo.canvas = null; photoFile.value = "";
  $("#photoResult").classList.add("hidden"); photoDrop.classList.remove("hidden");
  $("#photoBoxes").replaceChildren(); $("#photoFaces").replaceChildren();
}

function loadImage(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("This file can't be opened as an image. iPhone HEIC photos must be shared as JPG.")); };
    img.src = url;
  });
}

async function analysePhoto(file) {
  if (!file) return;
  msgIn("#photoMsg", "Finding faces…", true);
  try {
    await loadModels();
    const img = await loadImage(file);
    const scale = Math.min(1, 1600 / Math.max(img.naturalWidth, img.naturalHeight));   // keep memory sane on phones
    const c = document.createElement("canvas");
    c.width = Math.round(img.naturalWidth * scale); c.height = Math.round(img.naturalHeight * scale);
    c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
    const faces = (await detectPhoto(c)).sort((a, b) => a.box.x - b.box.x);
    if (!faces.length) { photoFile.value = ""; return msgIn("#photoMsg", "No faces found in this photo. Try a clearer or closer photo.", false); }
    photo.canvas = c;
    photo.faces = faces.map((f, i) => ({ ...f, index: i, match: state.faces.length ? matchFace(f.descriptor, state.faces, settings.threshold) : null, thumb: cropToDataUrl(c, f.box) }));
    renderPhotoFaces(c);
    msgIn("#photoMsg", "");
  } catch (e) { photoFile.value = ""; msgIn("#photoMsg", e.message, false); }
}

function renderPhotoFaces(c) {
  $("#photoPreview").src = c.toDataURL("image/jpeg", 0.85);
  const n = photo.faces.length;
  $("#photoSummary").textContent = n === 1 ? "Found 1 face. Type a name to add it." : `Found ${n} faces. Type a name next to each face you want to add.`;
  const boxes = [], items = [];
  const setSel = (i, on) => { boxes[i].classList.toggle("selected", on); items[i].classList.toggle("selected", on); };
  photo.faces.forEach((f, i) => {
    const known = f.match?.personId ? f.match : null;
    const box = el("div", { className: "pe-box" + (known ? " known" : "") }, el("span", { textContent: known ? `${i + 1} · ${known.name}` : String(i + 1) }));
    Object.assign(box.style, { left: `${(f.box.x / c.width) * 100}%`, top: `${(f.box.y / c.height) * 100}%`, width: `${(f.box.width / c.width) * 100}%`, height: `${(f.box.height / c.height) * 100}%` });
    const check = el("input", { type: "checkbox", title: "Add this face" });
    const name = el("input", { placeholder: known ? known.name : "Name", maxLength: 64, autocomplete: "off" });
    name.setAttribute("list", "peopleNames");
    const small = Math.min(f.box.width, f.box.height) < 60;
    const li = el("li", { className: "pe-face" }, check, el("img", { src: f.thumb, alt: "" }),
      el("div", { className: "pe-meta" }, el("span", { className: "badge", textContent: `Face ${i + 1}` }), name,
        known ? el("div", { className: "small hint-known", textContent: `Already known as ${known.name} (${pct(known.confidence)}). Adding it again improves accuracy.` }) : null,
        small ? el("div", { className: "small hint-warn", textContent: "Small face, so recognition may be less reliable." }) : null));
    name.addEventListener("input", () => { check.checked = name.value.trim() !== ""; setSel(i, check.checked); });
    check.addEventListener("change", () => {
      if (check.checked && !name.value.trim() && known) name.value = known.name;
      setSel(i, check.checked);
      if (check.checked && !name.value.trim()) name.focus();
    });
    box.addEventListener("click", () => { name.focus(); li.scrollIntoView({ block: "nearest" }); });
    for (const node of [li, box]) {
      node.addEventListener("mouseenter", () => box.classList.add("hover"));
      node.addEventListener("mouseleave", () => box.classList.remove("hover"));
    }
    f.check = check; f.nameInput = name;
    boxes.push(box); items.push(li);
  });
  $("#photoBoxes").replaceChildren(...boxes);
  $("#photoFaces").replaceChildren(...items);
  photoDrop.classList.add("hidden");
  $("#photoResult").classList.remove("hidden");
  if (n === 1) photo.faces[0].nameInput.focus();
}

$("#photoSave").addEventListener("click", async (e) => {
  const picked = photo.faces.filter((f) => f.check.checked);
  if (!picked.length) return msgIn("#photoMsg", "Type a name next to at least one face.", false);
  let items;
  try {
    items = picked.map((f) => {
      if (!f.nameInput.value.trim()) throw new Error(`Face ${f.index + 1} is ticked but has no name.`);
      return { name: cleanName(f.nameInput.value), descriptor: packDescriptor(f.descriptor), photo: f.thumb };
    });
  } catch (err) { return msgIn("#photoMsg", err.message, false); }
  e.target.disabled = true;
  try {
    await state.store.addFaces(items);
    resetPhoto();
    msgIn("#photoMsg", `Added ${items.length} face${items.length === 1 ? "" : "s"}: ${[...new Set(items.map((i) => i.name))].join(", ")}.`, true);
  } catch (err) { msgIn("#photoMsg", `Couldn't save: ${err.code || err.message}`, false); }
  finally { e.target.disabled = false; }
});
$("#photoReset").addEventListener("click", () => { resetPhoto(); msgIn("#photoMsg", ""); photoFile.click(); });
photoFile.addEventListener("change", () => analysePhoto(photoFile.files[0]));
["dragenter", "dragover"].forEach((t) => photoDrop.addEventListener(t, (e) => { e.preventDefault(); photoDrop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => photoDrop.addEventListener(t, (e) => { e.preventDefault(); photoDrop.classList.remove("over"); }));
photoDrop.addEventListener("drop", (e) => analysePhoto(e.dataTransfer.files[0]));

// ---- capture from the live camera -------------------------------------------
$("#captureForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  let name;
  try { name = cleanName($("#captureName").value); } catch (err) { return msgIn("#captureMsg", err.message, false); }
  if (!cam.running) return msgIn("#captureMsg", "Start the camera first (▶ Start camera above).", false);
  const btn = e.submitter; btn.disabled = true;
  try {
    const frame = grabFrame();
    const faces = await detectPhoto(frame);
    if (!faces.length) return msgIn("#captureMsg", "No face visible right now. Face the camera and try again.", false);
    const f = faces.reduce((a, b) => (a.box.width * a.box.height > b.box.width * b.box.height ? a : b));
    await state.store.addFaces([{ name, descriptor: packDescriptor(f.descriptor), photo: cropToDataUrl(frame, f.box) }]);
    msgIn("#captureMsg", `Captured a photo of ${name}. Capture 2–3 more from slightly different angles for best accuracy.`, true);
  } catch (err) { msgIn("#captureMsg", `Couldn't save: ${err.code || err.message}`, false); }
  finally { btn.disabled = false; }
});

// ---- add a face from a detection snapshot -----------------------------------
const nameDlg = $("#nameDialog");
let nameDlgEvent = null;
function openNameDialog(ev) {
  nameDlgEvent = ev;
  $("#nameDialogImg").src = ev.snapshot;
  $("#nameDialogInput").value = ev.name !== "Unknown" ? ev.name : "";
  msgIn("#nameDialogMsg", "");
  nameDlg.showModal();
  $("#nameDialogInput").focus();
}
$("#nameForm").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value !== "save") return;
  e.preventDefault();
  let name;
  try { name = cleanName($("#nameDialogInput").value); } catch (err) { return msgIn("#nameDialogMsg", err.message, false); }
  const btn = $("#nameDialogSave"); btn.disabled = true;
  try {
    await state.store.addFaces([{ name, descriptor: nameDlgEvent.descriptor, photo: nameDlgEvent.snapshot }]);
    nameDlg.close();
    toast(`Added face to ${name}`);
  } catch (err) { msgIn("#nameDialogMsg", `Couldn't save: ${err.code || err.message}`, false); }
  finally { btn.disabled = false; }
});

// ============================================================================ tabs + settings
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${t.dataset.tab}`));
}));

const setDlg = $("#settingsDialog");
// The slider runs left = relaxed, right = strict, so it holds (0.98 - distance threshold).
$("#setThreshold").addEventListener("input", () => { $("#thresholdLabel").textContent = strictnessWord(0.98 - +$("#setThreshold").value); });
$("#setFps").addEventListener("input", () => { $("#fpsLabel").textContent = $("#setFps").value; });
$("#settingsBtn").addEventListener("click", () => {
  $("#setThreshold").value = (0.98 - settings.threshold).toFixed(2);
  $("#setFps").value = settings.fps;
  $("#setCooldown").value = settings.cooldown;
  $("#setMotion").checked = settings.motion;
  $("#setUnknown").checked = settings.unknown;
  $("#setCamName").value = settings.camName;
  $("#thresholdLabel").textContent = strictnessWord(settings.threshold);
  $("#fpsLabel").textContent = settings.fps;
  setDlg.showModal();
});
setDlg.addEventListener("close", () => {
  if (setDlg.returnValue !== "save") return;
  settings.threshold = Math.round((0.98 - +$("#setThreshold").value) * 100) / 100;
  settings.fps = +$("#setFps").value;
  settings.cooldown = Math.max(5, +$("#setCooldown").value || DEFAULTS.cooldown);
  settings.motion = $("#setMotion").checked;
  settings.unknown = $("#setUnknown").checked;
  settings.camName = $("#setCamName").value.trim();
  saveSettings();
  toast("Settings saved");
});

document.addEventListener("click", function askNotify() {
  if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
  document.removeEventListener("click", askNotify);
});

// ============================================================================ opening animation
function splash() {
  const sp = document.getElementById("splash");
  if (!sp) return;
  const img = sp.querySelector("img");
  let done = false;
  const remove = () => { if (!done) { done = true; sp.remove(); } };
  const play = () => sp.classList.add("play");
  if (img.complete && img.naturalWidth) play();
  else { img.addEventListener("load", play, { once: true }); img.addEventListener("error", remove, { once: true }); }
  sp.addEventListener("animationend", (e) => { if (e.target === sp) remove(); });
  sp.addEventListener("click", remove);
  setTimeout(remove, 6000);
}

main();
