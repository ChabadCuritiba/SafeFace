/* SafeFace dashboard - plain JavaScript, no build step. */
(() => {
  "use strict";

  // ------------------------------------------------------------------ config
  const LS = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };
  const cfg = {
    get apiBase() {
      const v = LS.get("safeface.apiBase") ?? (window.SAFEFACE_CONFIG || {}).apiBase ?? "";
      return v.replace(/\/+$/, "");
    },
    get apiKey() { return LS.get("safeface.apiKey") || ""; },
  };

  /** Absolute URL to the backend. withKey adds ?key= for <img>/WebSocket URLs. */
  function url(path, withKey = false) {
    const base = cfg.apiBase || window.location.origin;
    const u = new URL(path, base + "/");
    if (withKey && cfg.apiKey) u.searchParams.set("key", cfg.apiKey);
    return u.toString();
  }
  const mediaUrl = (p) => (p ? url(p, true) : "");

  async function api(path, opts = {}) {
    const headers = new Headers(opts.headers || {});
    if (cfg.apiKey) headers.set("X-API-Key", cfg.apiKey);
    const res = await fetch(url(path), { ...opts, headers });
    let body = null;
    try { body = await res.json(); } catch { /* empty body */ }
    if (!res.ok) {
      const d = body && body.detail;
      const msg = typeof d === "string" ? d : (d && d.message) || res.statusText;
      const err = new Error(msg || `HTTP ${res.status}`);
      err.status = res.status; err.body = body;
      throw err;
    }
    return body;
  }

  // ------------------------------------------------------------------ helpers
  const $ = (s) => document.querySelector(s);
  const el = (tag, props = {}, ...children) => {
    const n = Object.assign(document.createElement(tag), props);
    for (const c of children) if (c != null) n.append(c);
    return n;
  };
  const fmtTime = (ts) => new Date(ts * 1000).toLocaleString();
  const fmtAgo = (ts) => {
    const s = Math.round(Date.now() / 1000 - ts);
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return new Date(ts * 1000).toLocaleDateString();
  };
  const pct = (c) => `${(c * 100).toFixed(1)}%`;

  function toast(text, { img, kind = "" } = {}) {
    const t = el("div", { className: `toast ${kind}` },
      img ? el("img", { src: img, alt: "" }) : null, el("div", { textContent: text }));
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
    } catch { /* audio blocked until user interacts */ }
  }

  // ------------------------------------------------------------------ live feed
  const feed = $("#feed");
  function startFeed() {
    $("#feedError").classList.add("hidden");
    feed.src = url("/api/stream", true) + (cfg.apiKey ? "&" : "?") + "t=" + Date.now();
  }
  feed.addEventListener("error", () => {
    $("#feedError").classList.remove("hidden");
    setTimeout(startFeed, 3000);
  });

  $("#cameraForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const source = $("#cameraSource").value.trim();
    if (!source) return;
    try {
      await api("/api/camera", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ source }) });
      toast("Switching camera…");
      $("#cameraSource").value = "";
    } catch (err) { toast(`Camera switch failed: ${err.message}`, { kind: "error" }); }
  });

  // ------------------------------------------------------------------ status
  function renderStatus(s) {
    const pill = $("#statusPill");
    if (!s.camera.connected) {
      pill.className = "pill pill-warn";
      pill.textContent = "Camera offline";
    } else {
      pill.className = "pill pill-ok";
      pill.textContent = s.processing.active ? "Live · scanning" : "Live · idle";
    }
    pill.title = s.camera.error || s.camera.source;
    $("#camStats").textContent =
      `${s.camera.source} · cam ${s.camera.capture_fps} fps · recog ${s.processing.process_fps} fps` +
      ` · ${Math.round(s.processing.process_ms)} ms · ${s.known_embeddings} face embeddings`;
  }

  // ------------------------------------------------------------------ alerts
  const MAX_ALERTS = 50;
  function alertItem(ev, isNew) {
    const unknown = ev.name === "Unknown";
    const li = el("li", { className: (unknown ? "unknown " : "") + (isNew ? "new" : "") },
      el("img", { src: mediaUrl(ev.snapshot_url), alt: ev.name, loading: "lazy" }),
      el("div", {},
        el("div", { className: "name", textContent: ev.name }),
        el("div", { className: "small muted", textContent: `${pct(ev.confidence)} · ${fmtAgo(ev.timestamp)}` })),
      addFaceButton(ev, "+ Add"));
    li.dataset.ts = ev.timestamp;
    li.dataset.conf = ev.confidence;
    return li;
  }
  function addAlert(ev, isNew = true) {
    const list = $("#alerts");
    list.querySelector(".empty")?.remove();
    list.prepend(alertItem(ev, isNew));
    while (list.children.length > MAX_ALERTS) list.lastChild.remove();
  }
  setInterval(() => {  // refresh "x ago" labels
    for (const li of document.querySelectorAll("#alerts li[data-ts]")) {
      li.querySelector(".small").textContent = `${pct(+li.dataset.conf)} · ${fmtAgo(+li.dataset.ts)}`;
    }
  }, 15000);

  function onDetection(ev) {
    addAlert(ev);
    prependHistoryRow(ev);
    beep();
    toast(`${ev.name} detected (${pct(ev.confidence)})`, { img: mediaUrl(ev.snapshot_url), kind: ev.name === "Unknown" ? "unknown" : "" });
    if ("Notification" in window && Notification.permission === "granted" && document.hidden) {
      new Notification(`SafeFace: ${ev.name}`, { body: `${pct(ev.confidence)} at ${fmtTime(ev.timestamp)}`, icon: mediaUrl(ev.snapshot_url) });
    }
  }

  // ------------------------------------------------------------------ websocket
  let ws, wsTimer, pingTimer;
  function connectWs() {
    clearTimeout(wsTimer); clearInterval(pingTimer);
    if (ws) { ws.onclose = null; ws.close(); }
    const wsUrl = url("/ws", true).replace(/^http/, "ws");
    ws = new WebSocket(wsUrl);
    ws.onopen = () => { pingTimer = setInterval(() => ws.readyState === 1 && ws.send("ping"), 25000); };
    ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.type === "status") renderStatus(msg.status);
      else if (msg.type === "detection") onDetection(msg.event);
    };
    ws.onclose = (e) => {
      clearInterval(pingTimer);
      const pill = $("#statusPill");
      pill.className = "pill pill-off";
      pill.textContent = e.code === 4401 ? "Bad API key" : "Disconnected";
      if (e.code === 4401) { openSettings(); return; }
      wsTimer = setTimeout(connectWs, 3000);
    };
  }

  // ------------------------------------------------------------------ history
  function historyRow(ev) {
    return el("tr", {},
      el("td", {}, el("a", { href: mediaUrl(ev.snapshot_url), target: "_blank", rel: "noopener" },
        el("img", { src: mediaUrl(ev.snapshot_url), alt: "", loading: "lazy" }))),
      el("td", { textContent: ev.name }),
      el("td", { className: "conf", textContent: pct(ev.confidence) }),
      el("td", { textContent: fmtTime(ev.timestamp) }),
      el("td", { className: "muted", textContent: ev.camera || "" }),
      el("td", {}, addFaceButton(ev, "+ Add face")));
  }
  function prependHistoryRow(ev) {
    const f = $("#historyFilter").value.trim().toLowerCase();
    if (f && !ev.name.toLowerCase().includes(f)) return;
    $("#historyBody").prepend(historyRow(ev));
  }
  async function loadHistory() {
    const name = $("#historyFilter").value.trim();
    const q = new URLSearchParams({ limit: "200" });
    if (name) q.set("name", name);
    try {
      const events = await api(`/api/events?${q}`);
      const body = $("#historyBody");
      body.replaceChildren(...events.map(historyRow));
      if (!events.length) body.append(el("tr", {}, el("td", { colSpan: 6, className: "muted", textContent: "No events." })));
      return events;
    } catch (err) {
      if (err.status === 401) openSettings();
      return [];
    }
  }
  let filterTimer;
  $("#historyFilter").addEventListener("input", () => { clearTimeout(filterTimer); filterTimer = setTimeout(loadHistory, 300); });
  $("#refreshHistory").addEventListener("click", loadHistory);
  $("#clearHistory").addEventListener("click", async () => {
    if (!confirm("Delete ALL detection events and snapshots?")) return;
    await api("/api/events", { method: "DELETE" });
    $("#alerts").replaceChildren(el("li", { className: "empty muted", textContent: "No detections yet." }));
    loadHistory();
  });

  // ------------------------------------------------------------------ people
  async function loadPeople() {
    try {
      const people = await api("/api/people");
      $("#peopleCount").textContent = `(${people.length})`;
      $("#peopleNames").replaceChildren(...people.map((p) => el("option", { value: p.name })));
      $("#peopleList").replaceChildren(...people.map((p) => {
        const del = el("button", { title: `Remove ${p.name}`, textContent: "✕" });
        del.addEventListener("click", async () => {
          if (!confirm(`Remove ${p.name} and all their reference photos?`)) return;
          await api(`/api/people/${p.id}`, { method: "DELETE" });
          loadPeople();
        });
        return el("li", {},
          el("img", { src: mediaUrl(p.photo_url), alt: "" }),
          el("div", { className: "info" },
            el("div", { textContent: p.name }),
            el("div", { className: "small muted", textContent: `${p.photo_count} photo(s)` + (p.last_seen ? ` · seen ${fmtAgo(p.last_seen)}` : "") })),
          del);
      }));
      if (!people.length) $("#peopleList").append(el("li", { className: "muted", textContent: "Nobody enrolled yet." }));
    } catch (err) { if (err.status === 401) openSettings(); }
  }

  const drop = $("#dropZone"), files = $("#enrollFiles");
  const showFiles = () => {
    $("#dropText").textContent = files.files.length ? `${files.files.length} photo(s) selected` : "Drop reference photos here or click to browse";
  };
  files.addEventListener("change", showFiles);
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => { files.files = e.dataTransfer.files; showFiles(); });

  function enrollMessage(text, ok) {
    $("#enrollMsg").replaceChildren(el("div", { className: ok ? "ok" : "err", textContent: text }));
  }

  $("#enrollForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("#enrollName").value.trim();
    if (!name || !files.files.length) return enrollMessage("Enter a name and choose at least one photo.", false);
    const fd = new FormData();
    fd.append("name", name);
    for (const f of files.files) fd.append("files", f);
    const btn = e.submitter; btn.disabled = true;
    enrollMessage("Analysing photos…", true);
    try {
      const r = await api("/api/people", { method: "POST", body: fd });
      const failed = r.results.filter((x) => !x.ok).map((x) => `${x.file}: ${x.message}`);
      enrollMessage(`Enrolled ${r.name} with ${r.added} photo(s).` + (failed.length ? ` Skipped — ${failed.join("; ")}` : ""), true);
      e.target.reset(); showFiles(); loadPeople();
    } catch (err) {
      const details = err.body?.detail?.results?.map((x) => `${x.file}: ${x.message}`).join("; ");
      enrollMessage(`${err.message}${details ? ` (${details})` : ""}`, false);
    } finally { btn.disabled = false; }
  });

  $("#captureBtn").addEventListener("click", async (e) => {
    const name = $("#enrollName").value.trim();
    if (!name) return enrollMessage("Enter a name first, then face the camera.", false);
    const fd = new FormData(); fd.append("name", name);
    e.target.disabled = true;
    try {
      await api("/api/people/capture", { method: "POST", body: fd });
      enrollMessage(`Captured a reference photo for ${name}. Capture 2–3 more from different angles for best accuracy.`, true);
      loadPeople();
    } catch (err) { enrollMessage(err.message, false); }
    finally { e.target.disabled = false; }
  });

  // ------------------------------------------------------------------ add faces from a photo
  const photo = { uploadId: null, faces: [] };
  const photoFile = $("#photoFile"), photoDrop = $("#photoDrop");

  function photoMessage(text, ok) {
    $("#photoMsg").replaceChildren(...(text ? [el("div", { className: ok ? "ok" : "err", textContent: text })] : []));
  }
  function resetPhoto() {
    photo.uploadId = null; photo.faces = [];
    photoFile.value = "";
    $("#photoResult").classList.add("hidden");
    photoDrop.classList.remove("hidden");
    $("#photoBoxes").replaceChildren(); $("#photoFaces").replaceChildren();
  }

  async function analysePhoto(file) {
    if (!file) return;
    if (!file.type.startsWith("image/")) return photoMessage("That file isn't an image.", false);
    photoMessage("Finding faces…", true);
    const fd = new FormData(); fd.append("file", file);
    let r;
    try { r = await api("/api/photos/detect", { method: "POST", body: fd }); }
    catch (err) { photoFile.value = ""; return photoMessage(err.message, false); }
    if (!r.faces.length) {
      photoFile.value = "";
      return photoMessage("No faces found in this photo. Try a clearer or closer photo where faces are at least ~40 px wide.", false);
    }
    photo.uploadId = r.upload_id; photo.faces = r.faces;
    photoMessage("");
    $("#photoPreview").src = r.preview;
    $("#photoSummary").textContent = r.faces.length === 1
      ? "Found 1 face. Type a name to add it."
      : `Found ${r.faces.length} faces. Type a name next to each face you want to add.`;

    const boxes = [], items = [];
    const setSelected = (i, on) => { boxes[i].classList.toggle("selected", on); items[i].classList.toggle("selected", on); };
    const setHover = (i, on) => boxes[i].classList.toggle("hover", on);

    r.faces.forEach((f, i) => {
      const [x, y, w, h] = f.box;
      const box = el("div", { className: "pe-box" + (f.match ? " known" : ""), title: f.match ? f.match.name : `Face ${i + 1}` },
        el("span", { textContent: f.match ? `${i + 1} · ${f.match.name}` : String(i + 1) }));
      Object.assign(box.style, { left: `${x * 100}%`, top: `${y * 100}%`, width: `${w * 100}%`, height: `${h * 100}%` });

      const check = el("input", { type: "checkbox", title: "Add this face" });
      const name = el("input", { placeholder: f.match ? f.match.name : "Name", value: "", maxLength: 64 });
      name.setAttribute("list", "peopleNames");
      const hints = [];
      if (f.match) hints.push(el("div", { className: "small hint-known", textContent: `Already known as ${f.match.name} (${pct(f.match.confidence)}). Adding it again improves accuracy.` }));
      if (f.small) hints.push(el("div", { className: "small hint-warn", textContent: "Small face, so recognition may be less reliable." }));
      const li = el("li", { className: "pe-face" }, check, el("img", { src: f.thumb, alt: "" }),
        el("div", { className: "pe-meta" }, el("span", { className: "badge", textContent: `Face ${i + 1}` }), name, ...hints));

      name.addEventListener("input", () => { check.checked = name.value.trim() !== ""; setSelected(i, check.checked); });
      check.addEventListener("change", () => {
        if (check.checked && !name.value.trim() && f.match) name.value = f.match.name;  // tick a known face = add to that person
        setSelected(i, check.checked);
        if (check.checked && !name.value.trim()) name.focus();
      });
      box.addEventListener("click", () => { name.focus(); li.scrollIntoView({ block: "nearest" }); });
      for (const n of [li, box]) {
        n.addEventListener("mouseenter", () => setHover(i, true));
        n.addEventListener("mouseleave", () => setHover(i, false));
      }
      boxes.push(box); items.push(li);
      f._check = check; f._name = name;
    });
    $("#photoBoxes").replaceChildren(...boxes);
    $("#photoFaces").replaceChildren(...items);
    photoDrop.classList.add("hidden");
    $("#photoResult").classList.remove("hidden");
    if (r.faces.length === 1) { r.faces[0]._name.focus(); }
  }

  $("#photoSave").addEventListener("click", async (e) => {
    const picked = photo.faces.filter((f) => f._check.checked);
    if (!picked.length) return photoMessage("Type a name next to at least one face.", false);
    const missing = picked.filter((f) => !f._name.value.trim());
    if (missing.length) return photoMessage(`Face ${missing.map((f) => f.index + 1).join(", ")} is ticked but has no name.`, false);
    e.target.disabled = true;
    try {
      const r = await api(`/api/photos/${photo.uploadId}/enroll`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ faces: picked.map((f) => ({ index: f.index, name: f._name.value.trim() })) }),
      });
      const names = [...new Set(r.results.map((x) => x.name))].join(", ");
      resetPhoto();
      photoMessage(`Added ${r.added} face${r.added === 1 ? "" : "s"}: ${names}.`, true);
      loadPeople();
    } catch (err) {
      photoMessage(err.message, false);
      if (err.status === 410) resetPhoto();
    } finally { e.target.disabled = false; }
  });
  $("#photoReset").addEventListener("click", () => { resetPhoto(); photoMessage(""); photoFile.click(); });
  photoFile.addEventListener("change", () => analysePhoto(photoFile.files[0]));
  ["dragenter", "dragover"].forEach((t) => photoDrop.addEventListener(t, (e) => { e.preventDefault(); photoDrop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => photoDrop.addEventListener(t, (e) => { e.preventDefault(); photoDrop.classList.remove("over"); }));
  photoDrop.addEventListener("drop", (e) => analysePhoto(e.dataTransfer.files[0]));

  // ------------------------------------------------------------------ add a face from a detection snapshot
  const nameDlg = $("#nameDialog");
  let nameDlgEvent = null;
  function addFaceButton(ev, label) {
    if (!ev.snapshot_url || ev.id == null) return null;
    const b = el("button", { type: "button", className: "add-face-btn", textContent: label, title: "Add this face to the database" });
    b.addEventListener("click", (e) => { e.stopPropagation(); openNameDialog(ev); });
    return b;
  }
  function openNameDialog(ev) {
    nameDlgEvent = ev;
    $("#nameDialogImg").src = mediaUrl(ev.snapshot_url);
    $("#nameDialogInput").value = ev.name !== "Unknown" ? ev.name : "";
    $("#nameDialogMsg").replaceChildren();
    nameDlg.showModal();
    $("#nameDialogInput").focus();
  }
  $("#nameForm").addEventListener("submit", async (e) => {
    if (e.submitter && e.submitter.value !== "save") return;  // Cancel just closes
    e.preventDefault();
    const name = $("#nameDialogInput").value.trim();
    const msg = (t, ok) => $("#nameDialogMsg").replaceChildren(el("div", { className: ok ? "ok" : "err", textContent: t }));
    if (!name) return msg("Type a name.", false);
    const btn = $("#nameDialogSave"); btn.disabled = true;
    try {
      await api(`/api/events/${nameDlgEvent.id}/enroll`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }),
      });
      nameDlg.close();
      toast(`Added face to ${name}`);
      loadPeople();
    } catch (err) { msg(err.message, false); }
    finally { btn.disabled = false; }
  });

  // ------------------------------------------------------------------ tabs
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${t.dataset.tab}`));
    if (t.dataset.tab === "people") loadPeople();
  }));

  // ------------------------------------------------------------------ settings
  const dlg = $("#settingsDialog");
  function openSettings() {
    if (dlg.open) return;
    $("#apiBaseInput").value = cfg.apiBase;
    $("#apiKeyInput").value = cfg.apiKey;
    dlg.showModal();
  }
  $("#settingsBtn").addEventListener("click", openSettings);
  dlg.addEventListener("close", () => {
    if (dlg.returnValue !== "save") return;
    LS.set("safeface.apiBase", $("#apiBaseInput").value.trim());
    LS.set("safeface.apiKey", $("#apiKeyInput").value.trim());
    boot();
  });

  // ------------------------------------------------------------------ boot
  async function boot() {
    startFeed();
    connectWs();
    const events = await loadHistory();
    $("#alerts").replaceChildren();
    events.slice(0, 15).reverse().forEach((ev) => addAlert(ev, false));
    if (!events.length) $("#alerts").append(el("li", { className: "empty muted", textContent: "No detections yet." }));
    loadPeople();
  }

  document.addEventListener("click", function askNotify() {
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
    document.removeEventListener("click", askNotify);
  });

  // ------------------------------------------------------------------ opening animation
  (function splash() {
    const sp = document.getElementById("splash");
    if (!sp) return;
    const img = sp.querySelector("img");
    let done = false;
    const remove = () => { if (!done) { done = true; sp.remove(); } };
    const play = () => sp.classList.add("play");
    // Wait for the logo so the animation never plays on an empty box.
    if (img.complete && img.naturalWidth) play();
    else { img.addEventListener("load", play, { once: true }); img.addEventListener("error", remove, { once: true }); }
    sp.addEventListener("animationend", (e) => { if (e.target === sp) remove(); });
    sp.addEventListener("click", remove);           // click to skip
    setTimeout(remove, 6000);                        // safety net
  })();

  boot();
})();
