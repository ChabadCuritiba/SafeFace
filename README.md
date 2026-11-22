# SafeFace: real-time face recognition for security cameras

SafeFace watches a USB webcam or an RTSP IP camera and recognises enrolled people. Each time it sees someone it knows, it logs the event (name, time, confidence and a cropped snapshot) and pushes a live alert to a web dashboard.

- **Backend:** Python, FastAPI, OpenCV, InsightFace (ArcFace `buffalo_l`), ONNX Runtime
- **Storage:** SQLite for people, embeddings and events. Face embeddings sit in an in-memory NumPy vector index.
- **Real-time:** WebSocket for alerts and status, MJPEG for the annotated live video
- **Frontend:** plain HTML, CSS and JS with no build step. It can be deployed to **Firebase Hosting**.

---

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
                      ┌──────────── frontend (static, Firebase Hosting or served by FastAPI) ─────┐
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
├── backend/
│   ├── app/
│   │   ├── main.py         FastAPI routes, auth, MJPEG, WebSocket, static files
│   │   ├── config.py       all settings (env vars / backend/.env)
│   │   ├── camera.py       threaded USB/RTSP/file capture with auto-reconnect
│   │   ├── pipeline.py     motion gate → recognition → events → annotated stream
│   │   ├── recognizer.py   InsightFace wrapper + vector index
│   │   ├── database.py     SQLite schema and queries
│   │   └── hub.py          thread-safe WebSocket broadcaster
│   ├── requirements.txt
│   ├── .env.example
│   └── Dockerfile          for Docker / Google Cloud Run
├── frontend/               static dashboard (deployed to Firebase Hosting)
│   ├── index.html  styles.css  app.js
│   └── config.js           ← backend URL for the deployed site
├── firebase.json
├── run.sh / run.bat        one-command local start
└── README.md
```

---

## 2. Local setup

### Prerequisites

- **Python 3.10–3.12** recommended. 3.13 also works.
- A **C/C++ compiler**, because `insightface` builds a small extension during install:
  - Ubuntu/Debian: `sudo apt install build-essential python3-dev python3-venv`
  - macOS: `xcode-select --install`
  - Windows: install **"Visual Studio Build Tools" → "Desktop development with C++"**
- A webcam, for testing.

### Install and run

```bash
git clone <this repo> SafeFace && cd SafeFace
./run.sh            # Windows: run.bat
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

> **GPU (optional):** with an NVIDIA GPU and CUDA installed, run `pip uninstall onnxruntime && pip install onnxruntime-gpu`. SafeFace picks up the GPU automatically.

---

## 3. Testing with a standard webcam

1. **Start the server** with the default `CAMERA_SOURCE=0`, which is the first webcam. Your face should appear in the *Live camera* panel with a red **Unknown** box around it.
   - Is the feed black, or showing "NO SIGNAL"? Try `CAMERA_SOURCE=1`. On macOS, also allow camera access for your terminal under *System Settings → Privacy → Camera*.
2. **Enrol yourself.** Open **People database** and use any of these:
   - **Add faces from a photo:** choose any photo, even a group photo. Every face gets a numbered box; type a name next to the faces you want and click **Add selected faces**. Faces that are already enrolled show in green with their name.
   - **Capture from camera:** type your name and click **📸 Capture from camera** 2–3 times while turning your head slightly.
   - **Several photos of one person:** type a name, drop in 3–5 photos (clear, front-facing, different lighting and angles) and click **Upload & enrol**.
   - **From a detection:** click **+ Add face** on any row in *Detection history* or *Live alerts* to name that snapshot, for example to identify an "Unknown". Set `LOG_UNKNOWN=true` so unknown faces get logged.
3. **Walk out of frame and back in.** The box turns green with your name and a match percentage. A toast and a sound fire, and a new row with a snapshot appears in **Detection history** and **Live alerts**.
4. **Check the motion gate.** Sit perfectly still. After about 3 s the status pill changes to **Live · idle** and recognition stops. Move and it switches back to **scanning**.
5. **Test a second person.** Hold up a photo of someone else on your phone. Enrol them, then show the photo again.

No webcam? Point `CAMERA_SOURCE` at any video file (`CAMERA_SOURCE=/path/to/video.mp4`). It loops forever.

You can also exercise the API from a terminal:

```bash
curl localhost:8000/api/status
curl -F name="Jane Doe" -F files=@jane1.jpg -F files=@jane2.jpg localhost:8000/api/people
curl "localhost:8000/api/events?limit=5"
```

Interactive API docs are at **http://localhost:8000/docs**.

---

## 4. Switching to an RTSP security camera

1. **Find the camera's RTSP URL.** Common patterns:

   | Brand | URL |
   |---|---|
   | Hikvision | `rtsp://user:pass@IP:554/Streaming/Channels/102` (`101` = main, `102` = sub-stream) |
   | Dahua / Amcrest | `rtsp://user:pass@IP:554/cam/realmonitor?channel=1&subtype=1` |
   | Reolink | `rtsp://user:pass@IP:554/h264Preview_01_sub` |
   | TP-Link Tapo | `rtsp://user:pass@IP:554/stream2` (create a *camera account* in the app first) |
   | Intelbras | `rtsp://user:pass@IP:554/cam/realmonitor?channel=1&subtype=1` |
   | Generic ONVIF | check with the **ONVIF Device Manager** tool |

   URL-encode special characters in the password (`@` → `%40`, `#` → `%23`).

2. **Test the URL first** in VLC (*Media → Open Network Stream*) or with `ffplay "rtsp://..."`.

3. **Point SafeFace at it**, either way:
   - permanently: set `CAMERA_SOURCE=rtsp://...` in `backend/.env` and restart, or
   - at runtime: paste the URL into the *Camera source* box under the video and click **Switch**. This resets on restart.

**Tips**

- **Use the sub-stream** (640×360 to 720p). It is plenty for faces within about 5 m and much lighter to decode than 4K.
- RTSP runs over **TCP** by default here, which is reliable over Wi-Fi. If the stream drops, SafeFace reconnects automatically with backoff.
- Mount the camera at **face height**, around 1.6–2.2 m, pointing at a doorway. Steep overhead angles hurt accuracy a lot.
- Distant faces too small to recognise? Raise `DET_SIZE` to `800`, or use the main stream.

---

## 5. Deploying with Firebase

Firebase Hosting serves **static files only**. It can't run Python, OpenCV or a model, and it can't reach a camera on your LAN. Deployment is therefore split in two:

| Piece | Where it runs |
|---|---|
| Dashboard (`frontend/`) | **Firebase Hosting**: global CDN, free HTTPS, `your-project.web.app` |
| Recognition backend (`backend/`) | **Option A:** a PC or mini-PC on the same network as the cameras *(recommended)*. **Option B:** Google Cloud Run *(only if the camera is reachable from the internet)* |

The dashboard connects straight to the backend URL over HTTPS and WSS. A Hosting rewrite isn't used because Firebase Hosting rewrites don't carry WebSockets or long-lived MJPEG streams.

### Step 1: deploy the dashboard to Firebase Hosting

```bash
npm install -g firebase-tools
firebase login
# .firebaserc already points at the project: safeface-3bc6e
```

Edit `frontend/config.js` and set `apiBase` to your backend's public HTTPS URL, which you get in Step 2:

```js
window.SAFEFACE_CONFIG = { apiBase: "https://cam.example.com" };
```

```bash
firebase deploy --only hosting
```

You can also leave `apiBase` empty and enter the backend URL and API key in the dashboard's ⚙ **Settings** dialog. They are saved per browser.

### Step 2, Option A (recommended): backend on-site, exposed with HTTPS

Run the backend on a machine on the cameras' LAN. Any PC, a NUC, or a Jetson works; a Raspberry Pi 5 can handle `buffalo_s`.

1. Create `backend/.env`:
   ```ini
   CAMERA_SOURCE=rtsp://user:pass@192.168.1.64:554/Streaming/Channels/102
   API_KEY=<a long random string>     # e.g. python -c "import secrets;print(secrets.token_urlsafe(32))"
   CORS_ORIGINS=https://your-project.web.app,https://your-project.firebaseapp.com
   SERVE_FRONTEND=false
   ```
2. Start it with `./run.sh`, or with Docker:
   ```bash
   cd backend
   docker build -t safeface .
   docker run -d --restart unless-stopped --env-file .env -p 8000:8080 -v safeface-data:/data safeface
   ```
   (For a USB webcam inside Docker on Linux, add `--device /dev/video0`.)
3. Give it a public **HTTPS** URL. Browsers block an `https://` Firebase page from calling plain `http://`. The easiest route that needs no port-forwarding is **Cloudflare Tunnel**:
   ```bash
   cloudflared tunnel --url http://localhost:8000          # quick test URL
   # or a named tunnel on your own domain for production
   ```
   Tailscale Funnel, ngrok, or a reverse proxy (Caddy/nginx) with a certificate also work.
4. Put that URL in `frontend/config.js` (or the ⚙ Settings dialog), then redeploy the hosting.

### Step 2, Option B: backend on Google Cloud Run

This only makes sense if your camera's RTSP stream is reachable from the internet, for example through a VPN or a cloud NVR.

```bash
cd backend
gcloud run deploy safeface-backend \
  --source . --region us-central1 \
  --cpu 2 --memory 2Gi --timeout 3600 \
  --min-instances 1 --max-instances 1 --no-cpu-throttling \
  --allow-unauthenticated \
  --set-env-vars "CAMERA_SOURCE=rtsp://...,API_KEY=...,CORS_ORIGINS=https://your-project.web.app"
```

- `--min-instances 1 --no-cpu-throttling` keeps the recognition loop running when no browser is open. Expect to pay for an always-on instance.
- `--max-instances 1` matters: each instance runs its own camera loop and its own SQLite file.
- Cloud Run's disk is **ephemeral**, so events and enrolled faces are lost on redeploy. Mount a Cloud Storage bucket at `/data` (`--add-volume` / `--add-volume-mount`), or prefer Option A.

### Security checklist for an internet-exposed backend

- [ ] `API_KEY` is set. Without it, **anyone with the URL can watch your camera**.
- [ ] `CORS_ORIGINS` lists only your Firebase domains.
- [ ] The backend is served over HTTPS only.
- [ ] `backend/data/` is backed up and **never committed**. It holds biometric data, and `.gitignore` already excludes it.

---

## 6. Configuration reference

All settings are environment variables. You can also put them in `backend/.env`.

| Variable | Default | Meaning |
|---|---|---|
| `CAMERA_SOURCE` | `0` | Webcam index, `rtsp://…` URL, or video file path |
| `PROCESS_FPS` | `3` | Recognition passes per second |
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
| `SERVE_FRONTEND` | `true` | Serve `frontend/` at `/` (turn off when using Firebase) |
| `ONNX_PROVIDERS` | `CUDAExecutionProvider,CPUExecutionProvider` | Inference backends, in order of preference |

**Tuning `MATCH_THRESHOLD`**

| Problem | Fix |
|---|---|
| Strangers get labelled as someone you enrolled | Raise it to `0.5`–`0.55` |
| Enrolled people show as *Unknown* | Lower it to `0.38`–`0.42` and enrol more photos (different angles and lighting, with and without glasses) |

Typical scores are 0.6–0.9 for the same person and below 0.3 for different people.

---

## 7. API summary

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Liveness check (no auth) |
| GET | `/api/status` | Camera, FPS, processing time, number of embeddings |
| GET | `/api/stream` | MJPEG live video with overlays |
| GET | `/api/snapshot.jpg` | Single annotated frame |
| GET | `/api/people` | Enrolled people |
| POST | `/api/people` | Form fields `name` and `files` (one or more images): enrol or add photos |
| POST | `/api/people/capture` | Form field `name`: enrol from the current camera frame |
| POST | `/api/photos/detect` | Form field `file`: find every face in a photo, with boxes, thumbnails and existing matches |
| POST | `/api/photos/{upload_id}/enroll` | `{"faces":[{"index":0,"name":"Ana"}]}`: save the chosen faces from that photo |
| POST | `/api/events/{id}/enroll` | `{"name":"Ana"}`: add the face from a detection snapshot |
| DELETE | `/api/people/{id}` | Remove a person and their photos |
| GET | `/api/events?limit=&offset=&name=` | Detection history |
| DELETE | `/api/events` | Clear history and snapshots |
| POST | `/api/camera` | `{"source": "rtsp://..."}`: switch camera at runtime |
| WS | `/ws` | Pushes `{"type":"detection","event":{...}}` and `{"type":"status",...}` |

Auth: send the header `X-API-Key: <key>` or the query parameter `?key=<key>`. The query form is needed for `<img>` and WebSocket URLs.

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `pip install insightface` fails with a compiler error | Install the build tools from §2. On Windows, use Python 3.10–3.12. |
| "NO SIGNAL / Cannot open …" | Wrong webcam index or RTSP URL. Test the URL in VLC. Check the camera's username and password, and that port 554 is reachable. |
| Video lags several seconds behind | Use the camera's sub-stream. Lag shouldn't build up, because only the newest frame is kept. |
| High CPU | Lower `PROCESS_FPS` to 1–2, use `MODEL_NAME=buffalo_s` and `DET_SIZE=480`, and keep `MOTION_ENABLED=true` |
| Dashboard on Firebase shows "Disconnected" | Check that the backend URL is HTTPS and that `CORS_ORIGINS` includes your Firebase domain. Then check the API key in ⚙ Settings. |
| "Bad API key" | Re-enter the key in ⚙ Settings. It must match `API_KEY` on the backend. |
| "No face found" when enrolling | Use a clearer, larger, front-facing photo. Faces must be at least about 40 px wide. |
| "Not a valid image" for iPhone photos | HEIC isn't supported. Export or share the photo as JPG (on iPhone: *Settings → Camera → Formats → Most Compatible*). |

---

## 9. Privacy and legal note

Face templates are **biometric data**. Under Brazil's LGPD, as under the GDPR and similar laws, biometric data is *sensitive personal data*. Before deploying:

- Only enrol people who have given consent.
- Put up visible signage saying recognition is in use.
- Restrict access to the dashboard.
- Set a retention period and clear old events regularly.

All data stays on the backend machine, in `backend/data/`. No cloud face API is used.
