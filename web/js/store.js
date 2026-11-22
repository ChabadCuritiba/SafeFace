// Data layer. Two interchangeable stores with the same API:
//   FirebaseStore - Firestore, shared live across every device (used on Firebase Hosting)
//   LocalStore    - this browser only (used when Firebase isn't configured, e.g. local testing)
//
// API: onPeople(cb) / onFaces(cb) / onEvents(cb) -> unsubscribe
//      addFaces([{ name, descriptor, photo }])  - creates people by name as needed
//      deletePerson(id), addEvent(event), clearEvents()

const MAX_EVENTS = 200;
const nameKey = (n) => n.trim().replace(/\s+/g, " ").toLowerCase();

/** Firebase config: optional override in config.js, else Firebase Hosting's auto-config. */
export async function getFirebaseConfig() {
  const manual = (window.SAFEFACE_CONFIG || {}).firebase;
  if (manual && manual.apiKey) return manual;
  try {
    const r = await fetch("/__/firebase/init.json", { cache: "no-store" });
    if (r.ok) return await r.json();
  } catch { /* not on Firebase Hosting */ }
  return null;
}

// ============================================================================ Firebase
export class FirebaseStore {
  mode = "firebase";

  static async create(config) {
    const fb = await import("../vendor/firebase.js");
    const s = new FirebaseStore();
    s.fb = fb;
    s.app = fb.initializeApp(config);
    s.auth = fb.getAuth(s.app);
    s.db = fb.getFirestore(s.app);
    // Local testing against the Firebase emulators: open the site with ?emulators=1
    if (new URLSearchParams(location.search).has("emulators")) {
      fb.connectAuthEmulator(s.auth, `http://${location.hostname}:9099`, { disableWarnings: true });
      fb.connectFirestoreEmulator(s.db, location.hostname, 8080);
      // Test helper (emulator only): sign in as any Google account without the popup.
      window.__emulatorSignIn = (email) => fb.signInWithCredential(s.auth,
        fb.GoogleAuthProvider.credential(JSON.stringify({ sub: email, email, email_verified: true })));
    }
    s._people = [];
    return s;
  }

  /** cb(user|null) on every sign-in state change. */
  onAuth(cb) { return this.fb.onAuthStateChanged(this.auth, cb); }

  async signIn() {
    const provider = new this.fb.GoogleAuthProvider();
    provider.setCustomParameters({ prompt: "select_account" });
    try {
      await this.fb.signInWithPopup(this.auth, provider);
    } catch (e) {
      if (["auth/popup-blocked", "auth/operation-not-supported-in-this-environment"].includes(e.code)) {
        await this.fb.signInWithRedirect(this.auth, provider);
      } else throw e;
    }
  }

  signOut() { return this.fb.signOut(this.auth); }

  _listen(q, map, cb, onError) {
    return this.fb.onSnapshot(q, (snap) => cb(snap.docs.map((d) => map(d.id, d.data()))), onError);
  }

  onPeople(cb, onError) {
    const { collection, query, orderBy } = this.fb;
    return this._listen(query(collection(this.db, "people"), orderBy("nameLower")),
      (id, d) => ({ id, ...d }),
      (list) => { this._people = list; cb(list); }, onError);
  }

  onFaces(cb, onError) {
    return this._listen(this.fb.collection(this.db, "faces"), (id, d) => ({ id, ...d }), cb, onError);
  }

  onEvents(cb, onError) {
    const { collection, query, orderBy, limit } = this.fb;
    return this._listen(query(collection(this.db, "events"), orderBy("timestamp", "desc"), limit(MAX_EVENTS)),
      (id, d) => ({ id, ...d }), cb, onError);
  }

  /** Status written by the SafeFace PC server (cameras online, last heartbeat). cb(null) if none. */
  onPcStatus(cb, onError) {
    const { doc, onSnapshot } = this.fb;
    return onSnapshot(doc(this.db, "system", "pc"), (d) => cb(d.exists() ? d.data() : null), onError);
  }

  async addFaces(items) {
    const { doc, collection, writeBatch, serverTimestamp, increment } = this.fb;
    const batch = writeBatch(this.db);
    const created = new Map();
    for (const it of items) {
      const key = nameKey(it.name);
      let pid = created.get(key) || this._people.find((p) => p.nameLower === key)?.id;
      const isNew = !pid;
      if (isNew) {
        pid = doc(collection(this.db, "people")).id;
        created.set(key, pid);
        batch.set(doc(this.db, "people", pid), {
          name: it.name.trim(), nameLower: key, photo: it.photo, faceCount: 0, createdAt: serverTimestamp(),
        });
      }
      batch.set(doc(this.db, "people", pid), { faceCount: increment(1) }, { merge: true });
      batch.set(doc(collection(this.db, "faces")), {
        personId: pid, name: it.name.trim(), descriptor: it.descriptor, photo: it.photo, createdAt: serverTimestamp(),
      });
    }
    await batch.commit();
  }

  async deletePerson(id) {
    const { collection, query, where, getDocs, writeBatch, doc } = this.fb;
    const faces = await getDocs(query(collection(this.db, "faces"), where("personId", "==", id)));
    const batch = writeBatch(this.db);
    faces.forEach((d) => batch.delete(d.ref));
    batch.delete(doc(this.db, "people", id));
    await batch.commit();
  }

  async addEvent(ev) {
    const { addDoc, collection, doc, setDoc } = this.fb;
    await addDoc(collection(this.db, "events"), { ...ev, timestamp: Date.now() });
    if (ev.personId) await setDoc(doc(this.db, "people", ev.personId), { lastSeen: Date.now() }, { merge: true });
  }

  async clearEvents() {
    const { collection, getDocs, writeBatch } = this.fb;
    const snap = await getDocs(collection(this.db, "events"));
    for (let i = 0; i < snap.docs.length; i += 400) {
      const batch = writeBatch(this.db);
      snap.docs.slice(i, i + 400).forEach((d) => batch.delete(d.ref));
      await batch.commit();
    }
  }
}

// ============================================================================ Local
const LS_KEY = "safeface.local.v1";

export class LocalStore {
  mode = "local";

  constructor() {
    this.subs = { people: new Set(), faces: new Set(), events: new Set() };
    this.data = this._load();
    window.addEventListener("storage", (e) => { if (e.key === LS_KEY) { this.data = this._load(); this._emitAll(); } });
  }

  _load() {
    try {
      const d = JSON.parse(localStorage.getItem(LS_KEY) || "{}");
      return { people: d.people || [], faces: d.faces || [], events: d.events || [] };
    } catch { return { people: [], faces: [], events: [] }; }
  }

  _save() {
    for (let attempt = 0; attempt < 5; attempt++) {
      try { localStorage.setItem(LS_KEY, JSON.stringify(this.data)); return; }
      catch { this.data.events = this.data.events.slice(0, Math.floor(this.data.events.length / 2)); } // over quota: drop old events
    }
  }

  _views() {
    const people = this.data.people.map((p) => ({
      ...p, faceCount: this.data.faces.filter((f) => f.personId === p.id).length,
    })).sort((a, b) => a.nameLower.localeCompare(b.nameLower));
    return { people, faces: this.data.faces, events: this.data.events };
  }

  _emit(kind) { const v = this._views()[kind]; this.subs[kind].forEach((cb) => cb(v)); }
  _emitAll() { ["people", "faces", "events"].forEach((k) => this._emit(k)); }
  _on(kind, cb) { this.subs[kind].add(cb); cb(this._views()[kind]); return () => this.subs[kind].delete(cb); }

  onPeople(cb) { return this._on("people", cb); }
  onFaces(cb) { return this._on("faces", cb); }
  onEvents(cb) { return this._on("events", cb); }
  onPcStatus(cb) { cb(null); return () => {}; }

  async addFaces(items) {
    const id = () => Math.random().toString(36).slice(2, 12);
    for (const it of items) {
      const key = nameKey(it.name);
      let p = this.data.people.find((x) => x.nameLower === key);
      if (!p) {
        p = { id: id(), name: it.name.trim(), nameLower: key, photo: it.photo, createdAt: Date.now() };
        this.data.people.push(p);
      }
      this.data.faces.push({ id: id(), personId: p.id, name: p.name, descriptor: it.descriptor, photo: it.photo, createdAt: Date.now() });
    }
    this._save(); this._emitAll();
  }

  async deletePerson(pid) {
    this.data.people = this.data.people.filter((p) => p.id !== pid);
    this.data.faces = this.data.faces.filter((f) => f.personId !== pid);
    this._save(); this._emitAll();
  }

  async addEvent(ev) {
    const e = { id: Math.random().toString(36).slice(2, 12), ...ev, timestamp: Date.now() };
    this.data.events = [e, ...this.data.events].slice(0, MAX_EVENTS);
    const p = this.data.people.find((x) => x.id === ev.personId);
    if (p) p.lastSeen = e.timestamp;
    this._save(); this._emit("events"); this._emit("people");
  }

  async clearEvents() { this.data.events = []; this._save(); this._emit("events"); }
}
