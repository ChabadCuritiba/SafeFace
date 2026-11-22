# SafeFace: face recognition for security cameras

SafeFace recognises enrolled people on camera. Each time it sees someone, it logs the event (name, time, match score and a snapshot) and pushes a live alert. It comes in two forms:

| | **Part A: Website** (`web/`) | **Part B: PC server** (`backend/`) |
|---|---|---|
| Install | Nothing. Open **https://safeface-3bc6e.web.app** | Python on a PC near the cameras |
| Cameras | The camera of the device showing the page (laptop webcam, phone, USB webcam) | USB webcams **and RTSP IP cameras (Hikvision etc.)** |
| Runs | While the page is open | 24/7 |
| Data | Firebase Firestore, synced live to every signed-in device | SQLite on the PC |
| Recognition | In the browser (face-api / TensorFlow.js) | InsightFace ArcFace (more accurate) |

Browsers can't open RTSP streams and can't reach a private network, so **Hikvision cameras need Part B**.

---

# Part A: Website (no install)

## A1. How it works

```
 Browser (any phone / laptop)                                   Firebase (safeface-3bc6e)
 ┌───────────────────────────────────────────────────┐          ┌─────────────────────────────┐
 │ camera ─► motion gate ─► face-api detect +        │          │ Hosting: the site + models  │
 │           describe (128-d) ─► match vs. enrolled  │          │ Auth: Google sign-in        │
 │           faces ─► event + snapshot ──────────────┼─────────►│ Firestore: people, faces,   │
 │ live alerts / history / people ◄──────────────────┼──────────┤ events (live listeners)     │
 └───────────────────────────────────────────────────┘          └─────────────────────────────┘
```

- **Video never leaves the device.** Only enrolled face descriptors, small face thumbnails and detection events are stored in Firestore.
- **Everything is self-hosted on Firebase Hosting:** the AI models (about 12 MB, cached after the first visit), face-api and the Firebase SDK. No other CDNs are used.
- **Every signed-in device sees alerts live.** For example, a tablet at the door runs the camera while your phone gets the alerts.
- **Only approved Google accounts can see or change anything.** This is enforced by `firestore.rules`, not just hidden in the page.
- If the site is opened somewhere Firebase isn't configured, such as a local test server, it still works and saves data **on that device only**. A *This device only* badge shows when that's the case.

## A2. One-time Firebase setup (about 5 minutes)

In the [Firebase console](https://console.firebase.google.com/) open project **safeface-3bc6e**:

1. **Register a web app:** *Project settings* (gear icon) → *Your apps* → **Add app → Web** (`</>`). Give it any nickname and click Register. You can skip the code it shows; the site configures itself.
2. **Turn on Google sign-in:** *Build → Authentication → Get started → Sign-in method → Google → Enable*, choose a support email, and Save.
3. **Create the database:** *Build → Firestore Database → Create database*. Choose a location close to you (e.g. `southamerica-east1` for São Paulo) and start in **production mode**.
4. **Choose who may sign in:** edit `firestore.rules`. By default, any Google account on **@chabadcuritiba.com** is allowed. Add other addresses (e.g. Gmail) to `allowedEmails()`:
   ```
   function allowedEmails() {
     return ['someone@gmail.com', 'other@gmail.com'];
   }
   ```

## A3. Deploy

On a PC with [Node.js LTS](https://nodejs.org) and [Git](https://git-scm.com/download/win):

```
git clone -b claude/face-recognition-security-camera-5bpwi4 https://github.com/ChabadCuritiba/SafeFace.git
cd SafeFace
npm install -g firebase-tools
firebase login
firebase deploy --only hosting,firestore
```

`.firebaserc` already points at `safeface-3bc6e`. The deploy uploads the site **and** the security rules.

To update later:

```
git pull
firebase deploy --only hosting,firestore
```

## A4. Use it

1. Open **https://safeface-3bc6e.web.app** and **sign in with Google**.
2. Click **▶ Start camera** and allow camera access. Pick a different camera from the dropdown if you have several; on phones the back camera is used by default.
3. **People database** tab. Add faces in any of these ways:
   - **Add faces from a photo:** works with group photos. Every face is boxed and numbered; type names and click *Add selected faces*.
   - **Add from the live camera:** type a name, face the camera and capture 2–3 times.
   - **+ Add face** on any detection in the history.
4. When an enrolled person passes, you get a green box with their name, a sound and pop-up, and a history entry. **Every signed-in device** gets the alert.
5. **⚙ Settings** (saved per device):
   - match strictness;
   - checks per second;
   - seconds before logging the same person again;
   - only scan on motion;
   - log unknown faces;
   - camera name shown in history (e.g. "Front door").

Next time the page is opened on the same device, the camera starts automatically.

**Tips**
- Keep the tab open and visible. Browsers slow down background tabs. The page keeps the screen awake while the camera is on.
- For a fixed "door camera", use an old phone or tablet on a stand, plugged into power, with the page open.
- Faster devices check more often: a laptop easily does 3–5 checks per second, an older phone 1–2.

## A5. Test the website locally

The site is plain static files, so you can open it with any local web server. No Firebase is needed; data is kept on that device:

```
cd web
python -m http.server 8000
```

Then open http://localhost:8000. Cameras only work on `localhost` or `https://`.

To test against Firebase's local emulators (sign-in, Firestore and rules):

```
firebase emulators:start --only hosting,firestore,auth --project demo-safeface
```

Then open http://127.0.0.1:5000/?emulators=1

## A6. Troubleshooting

| Problem | Fix |
|---|---|
| "Google sign-in isn't enabled" | Step A2.2 |
| "Access denied … isn't allowed" | Add the email to `firestore.rules` (A2.4), then run `firebase deploy --only firestore` |
| "Couldn't load data from Firebase" | Create the Firestore database (A2.3), then deploy the rules |
| *This device only* badge on safeface-3bc6e.web.app | Register a web app (A2.1) and reload |
| "Camera permission was blocked" | Click the camera icon in the address bar → Allow, then Start |
| Very slow ("graphics acceleration unavailable") | Turn on hardware acceleration in the browser settings, or use a newer device |
| Photo won't open (iPhone) | HEIC isn't supported by most browsers. Share the photo as JPG, or set *Settings → Camera → Formats → Most Compatible* |

---

# Part B: PC server (for Hikvision / RTSP cameras)

The Python server runs 24/7 on a PC on the same network as the cameras and serves its own dashboard at http://localhost:8000. It watches **several cameras at once** (Hikvision cameras, NVR channels, any RTSP camera, USB webcams), and can **send every detection to the SafeFace website** so you get the alerts on any phone.

**Quick start on Windows**
1. `git clone -b claude/face-recognition-security-camera-5bpwi4 https://github.com/ChabadCuritiba/SafeFace.git C:\SafeFace`
2. Double-click **`C:\SafeFace\run.bat`**. It offers to install Python if needed, installs everything else (5–10 minutes the first time), and opens the dashboard.
3. Click **+ Add camera**, choose *Hikvision camera or NVR channel*, enter the IP, username, password and channel, then click **Test connection** and **Save**. Repeat for each camera.
4. *(Optional)* Turn on the website sync (§5) and auto-start (`install-autostart.bat`).

- **Backend:** Python, FastAPI, OpenCV, InsightFace (ArcFace `buffalo_l`), ONNX Runtime
- **Storage:** SQLite for people, embeddings and events. Face embeddings sit in an in-memory NumPy vector index.
- **Real-time:** WebSocket for alerts and status, MJPEG for the annotated live video
- **Dashboard:** plain HTML, CSS and JS in `backend/dashboard/`, served by the backend

## 1. Architecture

```
                      ┌──────────────────────── backend (FastAPI, Python) ────────────────────────┐
 USB webcam ─┐        │                                                                           │
             ├──────► │  Camera thread ──► latest frame only (no RTSP lag)                        │
 RTSP camera ┘        │        │                                                                  │
                      │        ▼   PROCESS_FPS (default 3/s)                                      │
                      │  Motion gate ──(no motion)──► skip, costs about 1 ms                     │
                      │        │ motion                                                           │
                      │        ▼                                                                  │
                      │  InsightFace: detect (SCRFD) + embed (ArcFace, 512-d)                     │
                      │        │                                                                  │
                      │        ▼                                                                  │
                      │  FaceIndex: cosine similarity vs. enrolled embeddings (NumPy matmul)      │
                      │        │ known face, outside its cooldown                                 │
                      │        ▼                                                                  │
                      │  Save crop → data/snapshots/   Insert row → SQLite   Hub.publish() ───┐   │
                      │                                                                       │   │
                      │  /api/stream  MJPEG with boxes and names (encoded once, shared)       │   │
                      │  /ws          WebSocket: detection + status messages ◄────────────────┘   │
                      │  /api/people  enrol from uploads or the live frame                        │
                      │  /api/events  history                                                     │
                      └───────────────────────────────────────────────────────────────────────────┘
                                         ▲  HTTPS / WSS  (X-API-Key)
                                         │
                      ┌──────────── dashboard (backend/dashboard, served by FastAPI) ─────────────┐
                      │  Live feed · Live alerts · Detection history · People database         │
                      └───────────────────────────────────────────────────────────────────────────┘
```

**How it keeps CPU and GPU use low**

| Technique | Where | Effect |
|---|---|---|
| Capture thread keeps only the newest frame | `camera.py` | No growing RTSP buffer, so you never process stale video |
| Recognition is capped at `PROCESS_FPS` (3/s) | `pipeline.py` | About 10× less work than processing every frame |
| Motion gate (downscaled frame difference) | `pipeline.py` | Recognition is skipped completely while the scene is static |
| Only the detection and recognition models load | `recognizer.py` | Age, gender and landmark models are skipped |
| Each annotated JPEG is encoded once for all viewers | `pipeline.get_jpeg` | 5 open dashboards cost the same as 1 |
| Per-person cooldown (`EVENT_COOLDOWN_SECONDS`) | `pipeline.py` | One log entry per visit instead of 3 per second |

**Project layout**

```
SafeFace/
├── web/                    Part A: the website (deployed to Firebase Hosting)
│   ├── index.html  styles.css  config.js
│   ├── js/app.js           camera, recognition loop, UI
│   ├── js/faces.js         face-api wrapper (detect, describe, match)
│   ├── js/store.js         Firestore + this-device-only storage
│   ├── vendor/             face-api + Firebase SDK (self-hosted)
│   └── models/             face detection / recognition models
├── firestore.rules         who may sign in and read or write data
├── firebase.json  .firebaserc
├── backend/                Part B: PC server
│   ├── app/
│   │   ├── main.py         FastAPI routes, auth, MJPEG, WebSocket, static files
│   │   ├── config.py       all settings (env vars / backend/.env)
│   │   ├── camera.py       threaded USB/RTSP/file capture with auto-reconnect
│   │   ├── pipeline.py     motion gate → recognition → events → annotated stream
│   │   ├── recognizer.py   InsightFace wrapper + vector index
│   │   ├── database.py     SQLite schema and queries
│   │   └── hub.py          thread-safe WebSocket broadcaster
│   ├── dashboard/          the server's web dashboard
│   ├── requirements.txt  .env.example  Dockerfile
├── run.sh / run.bat        one-command start of the PC server
└── README.md
```

---

## 2. Local setup

### Prerequisites

- **Python 3.11 or 3.12** recommended (3.10 and 3.13 also work). On Windows, `run.bat` offers to install it for you via `winget`.
- No compiler or Visual Studio Build Tools needed. Every package installs from ready-made wheels.
- A webcam for testing, or your IP cameras.

### Install and run

```bash
git clone -b claude/face-recognition-security-camera-5bpwi4 https://github.com/ChabadCuritiba/SafeFace.git
cd SafeFace
./run.sh            # Windows: double-click run.bat
```

Or set it up by hand:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # optional: edit settings
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000**.

On first start InsightFace downloads the `buffalo_l` model pack (about 280 MB) to `~/.insightface/models/`. Later starts take a few seconds.

**Start automatically with Windows:** double-click `install-autostart.bat`. SafeFace then starts minimised whenever you log in (undo with `remove-autostart.bat`). For an unattended PC, also turn on automatic Windows login and the BIOS option *"Restore on AC power loss"*, so it comes back after a power cut.

> **GPU (optional):** with an NVIDIA GPU and CUDA installed, run `pip uninstall onnxruntime && pip install onnxruntime-gpu`. SafeFace picks up the GPU automatically.

---

## 3. Testing with a standard webcam

1. **Start the server.** On first run it creates *Camera 1* from `CAMERA_SOURCE` (default `0`, the first webcam). Your face should appear in the *Cameras* panel with a red **Unknown** box around it.
   - Showing "NO SIGNAL"? Click ✎ on the tile, choose *USB webcam* and try number 1. On macOS, also allow camera access for your terminal under *System Settings → Privacy → Camera*.
2. **Enrol yourself.** Open **People database** and use any of these:
   - **Add faces from a photo:** choose any photo, even a group photo. Every face gets a numbered box; type a name next to the faces you want and click **Add selected faces**. Faces that are already enrolled show in green with their name.
   - **Capture from camera:** type your name, pick the camera and click **📸 Capture from** 2–3 times while turning your head slightly.
   - **Several photos of one person:** type a name, drop in 3–5 photos (clear, front-facing, different lighting and angles) and click **Upload & enrol**.
   - **From a detection:** click **+ Add face** on any row in *Detection history* or *Live alerts* to name that snapshot, for example to identify an "Unknown". Set `LOG_UNKNOWN=true` so unknown faces get logged.
3. **Walk out of frame and back in.** The box turns green with your name and a match percentage. A toast and a sound fire, and a new row with a snapshot appears in **Detection history** and **Live alerts**.
4. **Check the motion gate.** Sit perfectly still. After about 3 s the status pill changes to **Live · idle** and recognition stops. Move and it switches back to **scanning**.
5. **Test a second person.** Hold up a photo of someone else on your phone. Enrol them, then show the photo again.

No webcam? Add a camera of type *Other IP camera* and give it the path of any video file. It loops forever.

You can also exercise the API from a terminal:

```bash
curl localhost:8000/api/status
curl -F name="Jane Doe" -F files=@jane1.jpg -F files=@jane2.jpg localhost:8000/api/people
curl "localhost:8000/api/events?limit=5"
```

Interactive API docs are at **http://localhost:8000/docs**.

---

## 4. Adding cameras (Hikvision and others)

Cameras are managed in the dashboard and saved in the database. `CAMERA_SOURCE` in `.env` is only used to create *Camera 1* on the very first run.

**Hikvision: cameras or NVR channels**
1. Find the IP address: use Hikvision's free **SADP** tool, the NVR screen (*Menu → Camera → IP Camera*), or your router's device list. If the cameras plug into the back of an NVR, use the **NVR's** IP for all of them.
2. In the camera's (or NVR's) web page, set the **sub-stream to H.264** (not H.265/H.264+), 640×360–720p, 10–15 fps, and create a viewer-only user for SafeFace.
3. In SafeFace click **+ Add camera** → *Hikvision camera or NVR channel*:

   | Field | Camera on its own | Through an NVR |
   |---|---|---|
   | IP address | the camera's IP | the NVR's IP |
   | Channel | `1` | the camera's number on the NVR (`1`, `2`, `3`…) |
   | Stream | Sub-stream | Sub-stream |

4. Click **Test connection**. It tells you exactly what's wrong, or shows a preview frame:

   | Result | Meaning |
   |---|---|
   | ✓ Connected | Save it |
   | No answer / can't reach | Wrong IP, or this PC isn't on the cameras' network |
   | Port is closed | RTSP is disabled (*Network → Advanced → Integration Protocol*) or wrong port |
   | Wrong username or password | Careful: Hikvision locks the account for 30 minutes after several wrong tries |
   | Channel doesn't exist | Wrong channel number |
   | Login worked but no video | Set the stream to plain **H.264** |

Passwords with special characters (`@ # !`) are handled automatically. The saved password is never shown again in the dashboard: leave the field empty when editing to keep it.

**Other brands:** choose *Other IP camera (RTSP address)* and paste the address:

| Brand | RTSP address |
|---|---|
| Dahua / Amcrest / Intelbras | `rtsp://user:pass@IP:554/cam/realmonitor?channel=1&subtype=1` |
| Reolink | `rtsp://user:pass@IP:554/h264Preview_01_sub` |
| TP-Link Tapo | `rtsp://user:pass@IP:554/stream2` (create a *camera account* in the app first) |
| Generic ONVIF | check with the **ONVIF Device Manager** tool |

**How many cameras?** Every camera gets its own capture thread, and they share one face model. Recognition only runs while a camera sees motion, so the real limit is how many cameras see people *at the same time*. Roughly: a modern i5/i7/Ryzen handles 4–6 busy cameras (use `MODEL_NAME=buffalo_s` and `PROCESS_FPS=2` for more), and a PC with an NVIDIA GPU handles 15–30.

**Tips**

- **Use the sub-stream** (640×360 to 720p). It is plenty for faces within about 5 m and much lighter to decode than 4K.
- RTSP runs over **TCP** by default here, which is reliable over Wi-Fi. If the stream drops, SafeFace reconnects automatically with backoff.
- Mount the camera at **face height**, around 1.6–2.2 m, pointing at a doorway. Steep overhead angles hurt accuracy a lot.
- Distant faces too small to recognise? Raise `DET_SIZE` to `800`, or use the main stream.

---

## 5. Alerts on your phone: sync to the SafeFace website

The PC can send every detection (name, time, match score, camera, snapshot) and its camera status to Firebase. You then open **https://safeface-3bc6e.web.app** on any phone or computer, sign in, and see the PC's alerts live, with no tunnel or port-forwarding. Live video stays on your local network.

1. Do the one-time Firebase setup and deploy from **Part A** (A2–A3). Use the latest code so `firestore.rules` includes the `system` collection.
2. In the Firebase console: *Project settings → Service accounts → **Generate new private key***. A `.json` file downloads.
3. Rename it to **`firebase-key.json`** and put it in **`C:\SafeFace\backend\`**.
4. Restart `run.bat`. The window shows `Firebase sync on (project safeface-3bc6e)`, and the dashboard shows **website sync ✓**.

On the website you'll see a **PC: 3/3 cameras** badge, which turns into **PC offline** if the PC stops reporting for 5 minutes. PC detections appear in *Live alerts* and *Detection history*, with the camera name.

> ⚠️ `firebase-key.json` gives full control of your Firebase project. Keep it only on the SafeFace PC. Never email it or put it in git (`.gitignore` already excludes it). If it leaks, delete it under *Service accounts → Manage keys* and generate a new one.

Notes:
- People are enrolled **on the PC** for the PC cameras. The website's own people list is used by browser cameras only, because the two use different AI models.
- If the internet goes down, detections are kept in a queue and sent when it comes back. Everything still works locally.
- Free-plan usage: 2 writes per detection plus about 720 status updates per day, well within 20,000 writes/day.

## 6. Remote access to the PC dashboard (optional)

To see the PC's **live video** from outside the building, give the PC a secure public address.

1. In `backend/.env`, set a key so strangers can't open your cameras:
   ```ini
   API_KEY=<a long random string>     # e.g. python -c "import secrets;print(secrets.token_urlsafe(32))"
   ```
2. Start the server (`run.bat` / `./run.sh`), or use Docker:
   ```bash
   cd backend
   docker build -t safeface .
   docker run -d --restart unless-stopped --env-file .env -e SERVE_FRONTEND=true -e FRONTEND_DIR=/app/dashboard \
     -v "$PWD/dashboard:/app/dashboard" -p 8000:8080 -v safeface-data:/data safeface
   ```
3. Give the PC an HTTPS address. The easiest way, with no port-forwarding, is **Cloudflare Tunnel**:
   ```bash
   cloudflared tunnel --url http://localhost:8000          # quick test URL
   # or a named tunnel on your own domain for a permanent address
   ```
   Tailscale Funnel or a reverse proxy (Caddy/nginx) with a certificate also work.
4. Open the tunnel address in any browser. Enter the API key in ⚙ **Settings** when asked.

**Google Cloud Run (advanced).** This only makes sense if the camera's RTSP stream is reachable from the internet (VPN or cloud NVR). It needs the Blaze plan and costs roughly US$70–100/month for an always-on instance:

```bash
cd backend
gcloud run deploy safeface-backend \
  --source . --region southamerica-east1 \
  --cpu 2 --memory 2Gi --timeout 3600 \
  --min-instances 1 --max-instances 1 --no-cpu-throttling \
  --allow-unauthenticated \
  --set-env-vars "CAMERA_SOURCE=rtsp://...,API_KEY=..."
```

Cloud Run's disk is **ephemeral**: mount a Cloud Storage bucket at `/data`, or events and faces are lost on redeploy.

### Security checklist for an internet-exposed backend

- [ ] `API_KEY` is set. Without it, **anyone with the URL can watch your camera**.
- [ ] `CORS_ORIGINS` lists only the origins that need it (leave it unset if you only use the server's own dashboard).
- [ ] The backend is served over HTTPS only.
- [ ] `backend/data/` is backed up and **never committed**. It holds biometric data, and `.gitignore` already excludes it.

---

## 7. Configuration reference

All settings are environment variables. You can also put them in `backend/.env`.

| Variable | Default | Meaning |
|---|---|---|
| `CAMERA_SOURCE` | `0` | First-run only: source for *Camera 1* (cameras are then managed in the dashboard) |
| `FIREBASE_CREDENTIALS` | `backend/firebase-key.json` | Service-account key; when present, detections sync to the website |
| `PROCESS_FPS` | `3` | Recognition passes per second, per camera |
| `STREAM_FPS` | `12` | Frame rate of the browser video |
| `MOTION_ENABLED` | `true` | Only recognise while there's motion |
| `MOTION_THRESHOLD` | `0.004` | Fraction of pixels that must change. Raise it if trees or rain trigger it. |
| `MOTION_HOLD_SECONDS` | `3` | Keep scanning this long after the last motion or face |
| `MODEL_NAME` | `buffalo_l` | `buffalo_s` is about 3× faster and slightly less accurate |
| `DET_SIZE` | `640` | Detector input size. Smaller is faster; larger finds distant faces. |
| `MATCH_THRESHOLD` | `0.45` | Minimum cosine similarity to count as a match |
| `MIN_FACE_SIZE` | `40` | Ignore faces smaller than this many pixels |
| `EVENT_COOLDOWN_SECONDS` | `30` | Minimum gap between two log entries for the same person |
| `LOG_UNKNOWN` | `false` | Also log and alert on unrecognised faces |
| `API_KEY` | *(empty)* | Shared secret for the API, stream and WebSocket |
| `CORS_ORIGINS` | `*` | Allowed dashboard origins |
| `DATA_DIR` | `backend/data` | Where the SQLite DB, snapshots and reference faces live |
| `SERVE_FRONTEND` | `true` | Serve the dashboard (`backend/dashboard/`) at `/` |
| `ONNX_PROVIDERS` | `CUDAExecutionProvider,CPUExecutionProvider` | Inference backends, in order of preference |

**Tuning `MATCH_THRESHOLD`**

| Problem | Fix |
|---|---|
| Strangers get labelled as someone you enrolled | Raise it to `0.5`–`0.55` |
| Enrolled people show as *Unknown* | Lower it to `0.38`–`0.42` and enrol more photos (different angles and lighting, with and without glasses) |

Typical scores are 0.6–0.9 for the same person and below 0.3 for different people.

---

## 8. API summary

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Liveness check (no auth) |
| GET | `/api/status` | Camera, FPS, processing time, number of embeddings |
| GET | `/api/cameras` | List cameras (passwords hidden) |
| POST | `/api/cameras` | Add: `{"name":"Door","hikvision":{"ip":"192.168.1.64","username":"admin","password":"…","channel":1}}` or `{"name":"…","source":"rtsp://…"}` |
| PATCH | `/api/cameras/{id}` | Edit name / address / `enabled` (empty password = keep) |
| DELETE | `/api/cameras/{id}` | Remove a camera |
| POST | `/api/cameras/test` | Diagnose a camera before saving; returns a preview frame |
| GET | `/api/cameras/{id}/stream` | MJPEG live video with overlays |
| GET | `/api/cameras/{id}/snapshot.jpg` | Single annotated frame |
| GET | `/api/people` | Enrolled people |
| POST | `/api/people` | Form fields `name` and `files` (one or more images): enrol or add photos |
| POST | `/api/people/capture` | Form fields `name`, `camera_id`: enrol from a camera's current frame |
| POST | `/api/photos/detect` | Form field `file`: find every face in a photo, with boxes, thumbnails and existing matches |
| POST | `/api/photos/{upload_id}/enroll` | `{"faces":[{"index":0,"name":"Ana"}]}`: save the chosen faces from that photo |
| POST | `/api/events/{id}/enroll` | `{"name":"Ana"}`: add the face from a detection snapshot |
| DELETE | `/api/people/{id}` | Remove a person and their photos |
| GET | `/api/events?limit=&offset=&name=` | Detection history |
| DELETE | `/api/events` | Clear history and snapshots |
| WS | `/ws` | Pushes `{"type":"detection","event":{...}}` and `{"type":"status",...}` (all cameras) |

Auth: send the header `X-API-Key: <key>` or the query parameter `?key=<key>`. The query form is needed for `<img>` and WebSocket URLs.

---

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| Install fails | Check the internet connection and run `run.bat` again; it continues where it stopped. Use Python 3.11 or 3.12. |
| "NO SIGNAL / Cannot open …" | Click ✎ on the camera → **Test connection** for the exact reason (§4) |
| Dashboard says "website sync offline" | No internet, or `firebase-key.json` belongs to a different project. Detections are queued and sent later. |
| Video lags several seconds behind | Use the camera's sub-stream. Lag shouldn't build up, because only the newest frame is kept. |
| High CPU | Lower `PROCESS_FPS` to 1–2, use `MODEL_NAME=buffalo_s` and `DET_SIZE=480`, and keep `MOTION_ENABLED=true` |
| Dashboard shows "Disconnected" | Check that `run.bat` is still running. Then check the API key in ⚙ Settings. |
| "Bad API key" | Re-enter the key in ⚙ Settings. It must match `API_KEY` on the backend. |
| "No face found" when enrolling | Use a clearer, larger, front-facing photo. Faces must be at least about 40 px wide. |
| "Not a valid image" for iPhone photos | HEIC isn't supported. Export or share the photo as JPG (on iPhone: *Settings → Camera → Formats → Most Compatible*). |

---

## 10. Privacy and legal note

Face templates are **biometric data**. Under Brazil's LGPD, as under the GDPR and similar laws, biometric data is *sensitive personal data*. Before deploying:

- Only enrol people who have given consent.
- Put up visible signage saying recognition is in use.
- Restrict access to the dashboard.
- Set a retention period and clear old events regularly.

Where the data lives: with the PC server, everything stays on that PC in `backend/data/`. With the website, video stays on the device, while face descriptors, small face thumbnails and detection events are stored in your own Firebase project and are readable only by the accounts allowed in `firestore.rules`. No third-party face API is used in either mode.
