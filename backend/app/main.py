"""FastAPI application: REST API, MJPEG live stream, WebSocket alerts, static UI."""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import settings
from .database import Database
from .hub import Hub
from .pipeline import Pipeline, crop_face, event_to_json
from .recognizer import FaceEngine, FaceIndex

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("safeface")

MAX_UPLOAD_BYTES = 15 * 1024 * 1024


class State:
    db: Database
    index: FaceIndex
    engine: FaceEngine
    hub: Hub
    pipeline: Pipeline


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    state.db = Database(settings.db_path)
    state.index = FaceIndex()
    state.index.load(state.db.all_embeddings())
    state.hub = Hub()
    state.hub.bind_loop(asyncio.get_running_loop())
    state.engine = await asyncio.to_thread(
        FaceEngine, settings.model_name, settings.det_size, settings.det_threshold, settings.providers
    )
    state.pipeline = Pipeline(settings, state.db, state.engine, state.index, state.hub)
    state.pipeline.start()
    if not settings.api_key:
        log.warning("API_KEY is not set - anyone who can reach this server can view the camera.")
    log.info("SafeFace ready.")
    yield
    state.pipeline.stop()


app = FastAPI(title="SafeFace", version="1.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- auth
class ApiKeyMiddleware:
    """Optional shared-secret auth. Accepts header X-API-Key or ?key= (needed
    for <img> MJPEG and WebSocket URLs, which can't carry custom headers)."""

    PROTECTED = ("/api", "/media", "/ws")
    PUBLIC = ("/api/health",)

    def __init__(self, app, key: str):
        self.app, self.key = app, key

    async def __call__(self, scope, receive, send):
        if (
            self.key
            and scope["type"] in ("http", "websocket")
            and scope.get("method") != "OPTIONS"
            and scope["path"].startswith(self.PROTECTED)
            and scope["path"] not in self.PUBLIC
        ):
            headers = dict(scope.get("headers") or [])
            provided = headers.get(b"x-api-key", b"").decode()
            if not provided:
                provided = parse_qs(scope.get("query_string", b"").decode()).get("key", [""])[0]
            if not hmac.compare_digest(provided.encode(), self.key.encode()):
                if scope["type"] == "websocket":
                    # Accept then close with 4401 so the browser can see *why* it failed.
                    await receive()  # websocket.connect
                    await send({"type": "websocket.accept"})
                    await send({"type": "websocket.close", "code": 4401})
                    return
                body = json.dumps({"detail": "Invalid or missing API key"}).encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


app.add_middleware(ApiKeyMiddleware, key=settings.api_key)
app.add_middleware(  # added last = outermost, so even 401s carry CORS headers
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- helpers
def decode_image(data: bytes) -> np.ndarray | None:
    if not data or len(data) > MAX_UPLOAD_BYTES:
        return None
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


def clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not 1 <= len(name) <= 64:
        raise HTTPException(400, "Name must be 1-64 characters")
    if name.lower() == "unknown":
        raise HTTPException(400, '"Unknown" is reserved for unrecognised faces')
    return name


def person_to_json(p: dict) -> dict:
    p = dict(p)
    p["photo_url"] = f"/media/{p['photo_path']}" if p.get("photo_path") else None
    return p


def save_face(person_id: int, img: np.ndarray, embedding: np.ndarray, bbox: tuple[int, int, int, int]) -> None:
    rel = Path("faces") / str(person_id) / f"{uuid.uuid4().hex[:12]}.jpg"
    (settings.data_dir / rel).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(settings.data_dir / rel), crop_face(img, bbox), [cv2.IMWRITE_JPEG_QUALITY, 92])
    state.db.add_embedding(person_id, embedding, rel.as_posix())


def enroll_image(person_id: int, img: np.ndarray) -> tuple[bool, str]:
    result = state.engine.largest_face_embedding(img)
    if result is None:
        return False, "no face found"
    embedding, bbox = result
    save_face(person_id, img, embedding, bbox)
    return True, "ok"


def jpeg_data_url(img: np.ndarray, max_side: int, quality: int = 85) -> str:
    h, w = img.shape[:2]
    scale = min(1.0, max_side / max(h, w))
    if scale < 1:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


class PhotoCache:
    """Holds analysed photos briefly so the user can pick and name faces
    without re-uploading. In-memory: fine for a single backend process."""

    TTL, MAX = 15 * 60, 20

    def __init__(self) -> None:
        self._items: dict[str, tuple[float, np.ndarray, list]] = {}
        self._lock = threading.Lock()

    def put(self, img: np.ndarray, faces: list) -> str:
        key = uuid.uuid4().hex
        with self._lock:
            now = time.time()
            for k in [k for k, v in self._items.items() if now - v[0] > self.TTL]:
                del self._items[k]
            while len(self._items) >= self.MAX:
                del self._items[min(self._items, key=lambda k: self._items[k][0])]
            self._items[key] = (now, img, faces)
        return key

    def get(self, key: str):
        with self._lock:
            item = self._items.get(key)
        if not item or time.time() - item[0] > self.TTL:
            return None
        return item[1], item[2]


photo_cache = PhotoCache()


def remove_files(rel_paths: list[str]) -> None:
    for rel in rel_paths:
        try:
            (settings.data_dir / rel).unlink(missing_ok=True)
        except OSError:
            pass


# --------------------------------------------------------------------------- routes
@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/status")
def status():
    return state.pipeline.status()


@app.get("/api/stream")
async def stream(request: Request):
    """MJPEG stream - use directly as <img src="/api/stream">."""
    delay = 1.0 / max(settings.stream_fps, 1)

    async def frames():
        last = None
        while not await request.is_disconnected():
            jpg = await asyncio.to_thread(state.pipeline.get_jpeg)
            if jpg and jpg is not last:
                last = jpg
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                       + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
            await asyncio.sleep(delay)

    return StreamingResponse(
        frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/api/snapshot.jpg")
async def snapshot():
    jpg = await asyncio.to_thread(state.pipeline.get_jpeg)
    return StreamingResponse(iter([jpg]), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ---- people / enrolment ------------------------------------------------------
@app.get("/api/people")
def list_people():
    return [person_to_json(p) for p in state.db.list_people()]


@app.post("/api/people")
def add_person(name: str = Form(...), files: list[UploadFile] = File(...)):
    """Enrol a person from one or more reference photos (more angles = better)."""
    name = clean_name(name)
    person_id = state.db.get_or_create_person(name)
    results = []
    for f in files:
        img = decode_image(f.file.read())
        if img is None:
            results.append({"file": f.filename, "ok": False, "message": "not a valid image (or > 15 MB)"})
            continue
        ok, msg = enroll_image(person_id, img)
        results.append({"file": f.filename, "ok": ok, "message": msg})

    added = sum(r["ok"] for r in results)
    if added == 0 and not any(p["id"] == person_id and p["photo_count"] for p in state.db.list_people()):
        state.db.delete_person(person_id)
        raise HTTPException(422, detail={"message": "No usable face found in any photo", "results": results})
    state.index.load(state.db.all_embeddings())
    return {"person_id": person_id, "name": name, "added": added, "results": results}


@app.post("/api/people/capture")
def capture_person(name: str = Form(...)):
    """Enrol a person from the current live camera frame (great for webcam testing)."""
    name = clean_name(name)
    frame = state.pipeline.current_frame()
    if frame is None:
        raise HTTPException(503, "Camera has no frame yet")
    if state.engine.largest_face_embedding(frame) is None:
        raise HTTPException(422, "No face visible in the camera right now")
    person_id = state.db.get_or_create_person(name)
    enroll_image(person_id, frame)
    state.index.load(state.db.all_embeddings())
    return {"person_id": person_id, "name": name, "added": 1}


@app.post("/api/photos/detect")
def detect_photo(file: UploadFile = File(...)):
    """Step 1 of 'add faces from a photo': find every face and say who it already matches."""
    img = decode_image(file.file.read())
    if img is None:
        raise HTTPException(400, "Not a valid image (JPG/PNG/WebP, max 15 MB). iPhone HEIC photos must be exported as JPG.")
    faces = sorted(state.engine.detect(img, min_face_size=20), key=lambda f: f.bbox[0])  # left to right
    h, w = img.shape[:2]
    out = []
    for i, f in enumerate(faces):
        pid, name, conf = state.index.match(f.embedding, settings.match_threshold)
        x1, y1, x2, y2 = f.bbox
        out.append({
            "index": i,
            "box": [x1 / w, y1 / h, (x2 - x1) / w, (y2 - y1) / h],  # normalised x, y, width, height
            "det_score": round(f.det_score, 3),
            "match": {"person_id": pid, "name": name, "confidence": round(conf, 4)} if pid is not None else None,
            "thumb": jpeg_data_url(crop_face(img, f.bbox), 160),
            "small": min(x2 - x1, y2 - y1) < 60,
        })
    upload_id = photo_cache.put(img, faces)
    return {"upload_id": upload_id, "width": w, "height": h, "preview": jpeg_data_url(img, 1200), "faces": out}


class FaceAssignment(BaseModel):
    index: int
    name: str


class PhotoEnrollBody(BaseModel):
    faces: list[FaceAssignment]


@app.post("/api/photos/{upload_id}/enroll")
def enroll_photo_faces(upload_id: str, body: PhotoEnrollBody):
    """Step 2: save the faces the user picked, each under the name they typed."""
    cached = photo_cache.get(upload_id)
    if cached is None:
        raise HTTPException(410, "This photo has expired - please upload it again")
    img, faces = cached
    if not body.faces:
        raise HTTPException(400, "Pick at least one face")
    names = [clean_name(a.name) for a in body.faces]  # validate everything before saving anything
    for a in body.faces:
        if not 0 <= a.index < len(faces):
            raise HTTPException(400, f"Unknown face #{a.index}")
    results = []
    for a, name in zip(body.faces, names):
        f = faces[a.index]
        person_id = state.db.get_or_create_person(name)
        save_face(person_id, img, f.embedding, f.bbox)
        results.append({"index": a.index, "name": name, "person_id": person_id})
    state.index.load(state.db.all_embeddings())
    return {"added": len(results), "results": results}


@app.delete("/api/people/{person_id}")
def delete_person(person_id: int):
    if not state.db.get_person(person_id):
        raise HTTPException(404, "Person not found")
    remove_files(state.db.delete_person(person_id))
    shutil.rmtree(settings.faces_dir / str(person_id), ignore_errors=True)
    state.index.load(state.db.all_embeddings())
    return {"deleted": person_id}


# ---- events ------------------------------------------------------------------
@app.get("/api/events")
def list_events(limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), name: str | None = None):
    return [event_to_json(e) for e in state.db.list_events(limit, offset, name)]


class NameBody(BaseModel):
    name: str


@app.post("/api/events/{event_id}/enroll")
def enroll_from_event(event_id: int, body: NameBody):
    """Add the face from a detection snapshot to the database (e.g. name an 'Unknown')."""
    name = clean_name(body.name)
    event = state.db.get_event(event_id)
    if not event or not event.get("snapshot_path"):
        raise HTTPException(404, "Event or snapshot not found")
    img = cv2.imread(str(settings.data_dir / event["snapshot_path"]))
    if img is None:
        raise HTTPException(404, "Snapshot file is missing")
    result = state.engine.largest_face_embedding(img)
    if result is None:
        raise HTTPException(422, "Couldn't find a clear face in this snapshot")
    person_id = state.db.get_or_create_person(name)
    save_face(person_id, img, *result)
    state.index.load(state.db.all_embeddings())
    return {"person_id": person_id, "name": name, "added": 1}


@app.delete("/api/events")
def clear_events():
    remove_files(state.db.clear_events())
    return {"cleared": True}


# ---- camera ------------------------------------------------------------------
class CameraBody(BaseModel):
    source: str


@app.post("/api/camera")
def set_camera(body: CameraBody):
    """Switch source at runtime, e.g. {"source": "0"} or {"source": "rtsp://..."}."""
    if not body.source.strip():
        raise HTTPException(400, "source is required")
    state.pipeline.switch_camera(body.source.strip())
    return state.pipeline.status()


# ---- websocket ---------------------------------------------------------------
@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await state.hub.connect(ws)
    try:
        await ws.send_json({"type": "status", "status": state.pipeline.status()})
        while True:
            await ws.receive_text()  # keep-alive pings from the browser
    except WebSocketDisconnect:
        pass
    finally:
        state.hub.disconnect(ws)


# ---- static files ------------------------------------------------------------
# Only the image folders are exposed - never the whole data dir (it holds the DB).
settings.ensure_dirs()
app.mount("/media/snapshots", StaticFiles(directory=settings.snapshots_dir), name="snapshots")
app.mount("/media/faces", StaticFiles(directory=settings.faces_dir), name="faces")
if settings.serve_frontend and settings.frontend_dir.is_dir():
    app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")
