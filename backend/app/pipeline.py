"""The real-time loop, one worker per camera:
    camera -> motion gate -> detect -> match -> log/alert

All workers share one face model and one face index. Two independent rates keep
CPU usage low while the video still looks smooth:
  * recognition runs at PROCESS_FPS (default 3/s) per camera, only while there is motion;
  * the browser stream is re-encoded at STREAM_FPS with the latest results drawn on top.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .camera import Camera, describe_source
from .config import Settings
from .database import Database
from .hub import Hub
from .recognizer import DetectedFace, FaceEngine, FaceIndex

log = logging.getLogger(__name__)

GREEN = (80, 200, 60)
RED = (60, 60, 230)
GREY = (170, 170, 170)  # face found by the tracker, name not checked yet
OVERLAY_TTL = 1.5  # seconds a box stays on screen after the last detection
STREAM_MAX_WIDTH = 1280  # frames wider than this are downscaled before JPEG encoding

# Called for every logged detection: (event dict, face crop as BGR image)
EventSink = Callable[[dict, np.ndarray], None]


def safe_filename(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")[:40] or "face"


def crop_face(frame: np.ndarray, bbox: tuple[int, int, int, int], margin: float = 0.35) -> np.ndarray:
    x1, y1, x2, y2 = bbox
    h, w = frame.shape[:2]
    mx, my = int((x2 - x1) * margin), int((y2 - y1) * margin)
    return frame[max(0, y1 - my): min(h, y2 + my), max(0, x1 - mx): min(w, x2 + mx)].copy()


def _best_match(bbox, prev: list[DetectedFace], used: set[int]) -> int | None:
    """Index of the previous face that this new box most likely belongs to."""
    x1, y1, x2, y2 = bbox
    cx, cy, size = (x1 + x2) / 2, (y1 + y2) / 2, max(x2 - x1, y2 - y1)
    best, best_score = None, 0.0
    for i, f in enumerate(prev):
        if i in used:
            continue
        a1, b1, a2, b2 = f.bbox
        iw, ih = max(0, min(x2, a2) - max(x1, a1)), max(0, min(y2, b2) - max(y1, b1))
        inter = iw * ih
        union = (x2 - x1) * (y2 - y1) + (a2 - a1) * (b2 - b1) - inter
        iou = inter / union if union else 0.0
        # fast movement can drop the overlap to zero: fall back to centre distance
        dist = ((cx - (a1 + a2) / 2) ** 2 + (cy - (b1 + b2) / 2) ** 2) ** 0.5
        closeness = max(0.0, 1 - dist / (1.5 * max(size, a2 - a1, b2 - b1)))
        score = max(iou, 0.5 * closeness)
        if score > best_score and score >= 0.15:
            best, best_score = i, score
    return best


def event_to_json(event: dict) -> dict:
    out = dict(event)
    out["snapshot_url"] = f"/media/{event['snapshot_path']}" if event.get("snapshot_path") else None
    return out


class CameraWorker:
    """Captures one camera and runs recognition on it."""

    def __init__(self, camera_id: int, name: str, source: str, cfg: Settings, db: Database,
                 engine: FaceEngine, index: FaceIndex, sinks: list[EventSink]):
        self.id, self.name, self.source = camera_id, name, source
        self.cfg, self.db, self.engine, self.index, self.sinks = cfg, db, engine, index, sinks
        self.camera = Camera(source, cfg.camera_width, cfg.camera_height)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._faces: list[DetectedFace] = []
        self._faces_time = 0.0
        self._faces_lock = threading.Lock()
        self._last_event: dict[str, float] = {}
        self._bg: np.ndarray | None = None
        self._last_activity = 0.0

        self._jpeg_cache: tuple[tuple, bytes] | None = None
        self._jpeg_lock = threading.Lock()

        self.stats = {"process_fps": 0.0, "process_ms": 0.0, "track_ms": 0.0, "motion": False, "active": False, "faces": 0}

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        self.camera.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"recognition-{self.id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.camera.stop()

    def status(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "source": describe_source(self.source),
            "connected": self.camera.connected,
            "error": self.camera.error,
            "capture_fps": round(self.camera.capture_fps, 1),
            "processing": {k: (round(v, 1) if isinstance(v, float) else v) for k, v in self.stats.items()},
        }

    def current_frame(self) -> np.ndarray | None:
        _, frame, _ = self.camera.read()
        return None if frame is None else frame.copy()

    # ------------------------------------------------------------------ motion
    def _motion(self, frame: np.ndarray) -> bool:
        small = cv2.resize(frame, (320, int(320 * frame.shape[0] / frame.shape[1])))
        gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (21, 21), 0).astype(np.float32)
        if self._bg is None or self._bg.shape != gray.shape:
            self._bg = gray
            return True
        delta = cv2.absdiff(gray, self._bg)
        cv2.accumulateWeighted(gray, self._bg, 0.25)
        changed = float(np.count_nonzero(delta > 25)) / delta.size
        return changed >= self.cfg.motion_threshold

    # ------------------------------------------------------------------ main loop
    def _run(self) -> None:
        rec_interval = 1.0 / max(self.cfg.process_fps, 0.1)
        tick = 1.0 / self.cfg.track_fps if self.cfg.track_fps > 0 else rec_interval
        tick = min(tick, rec_interval)
        last_id, passes, t_stats, last_rec = -1, 0, time.time(), 0.0
        while not self._stop.is_set():
            loop_start = time.time()
            frame_id, frame, _ = self.camera.read()

            if frame is not None and frame_id != last_id:
                last_id = frame_id
                motion = self._motion(frame) if self.cfg.motion_enabled else True
                if motion:
                    self._last_activity = loop_start
                active = motion or (loop_start - self._last_activity) < self.cfg.motion_hold_seconds
                self.stats["motion"], self.stats["active"] = motion, active

                if active and loop_start - last_rec >= rec_interval:
                    last_rec = loop_start
                    self._recognise(frame)
                    passes += 1
                elif active and self.cfg.track_fps > 0:
                    self._track(frame)

            now = time.time()
            if now - t_stats >= 2:
                self.stats["process_fps"] = passes / (now - t_stats)
                passes, t_stats = 0, now
            self._stop.wait(max(0.0, tick - (time.time() - loop_start)))

    def _recognise(self, frame: np.ndarray) -> None:
        """Full pass: detect + embed + match. Sets names and logs events."""
        t0 = time.time()
        try:
            faces = self.engine.detect(frame, self.cfg.min_face_size)
        except Exception:  # never let one bad frame kill the loop
            log.exception("Detection failed on %s", self.name)
            return
        for f in faces:
            f.person_id, f.name, f.confidence = self.index.match(f.embedding, self.cfg.match_threshold)
        self.stats["process_ms"] = (time.time() - t0) * 1000
        self.stats["faces"] = len(faces)
        if faces:
            self._last_activity = time.time()
        with self._faces_lock:
            self._faces, self._faces_time = faces, time.time()
        if faces:
            self._handle_events(frame, faces)

    def _track(self, frame: np.ndarray) -> None:
        """Fast pass between recognitions: find face positions only and move the
        existing boxes (keeping their names) so they follow people smoothly."""
        t0 = time.time()
        try:
            boxes = self.engine.detect_boxes(frame, self.cfg.track_size, self.cfg.min_face_size)
        except Exception:
            log.exception("Tracking failed on %s", self.name)
            return
        self.stats["track_ms"] = (time.time() - t0) * 1000
        with self._faces_lock:
            prev = list(self._faces) if time.time() - self._faces_time < OVERLAY_TTL else []
        updated, used = [], set()
        for bbox, score in boxes:
            j = _best_match(bbox, prev, used)
            if j is not None:
                used.add(j)
                updated.append(replace(prev[j], bbox=bbox))
            else:
                updated.append(DetectedFace(bbox, score, None, name="…", pending=True))
        if updated:
            self._last_activity = time.time()
        with self._faces_lock:
            self._faces, self._faces_time = updated, time.time()

    def _handle_events(self, frame: np.ndarray, faces: list[DetectedFace]) -> None:
        now = time.time()
        for f in faces:
            if f.person_id is None and not self.cfg.log_unknown:
                continue
            key = f"p{f.person_id}" if f.person_id is not None else "unknown"
            if now - self._last_event.get(key, 0) < self.cfg.event_cooldown_seconds:
                continue
            self._last_event[key] = now

            crop = crop_face(frame, f.bbox)
            day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
            rel = Path("snapshots") / day / f"{datetime.fromtimestamp(now):%H%M%S}_{int(now * 1000) % 1000:03d}_c{self.id}_{safe_filename(f.name)}.jpg"
            (self.cfg.data_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(self.cfg.data_dir / rel), crop, [cv2.IMWRITE_JPEG_QUALITY, 90])

            event = self.db.add_event(f.person_id, f.name, round(f.confidence, 4), rel.as_posix(), self.name, now)
            event["camera_id"] = self.id
            log.info("Detected %s (%.2f) on %s", f.name, f.confidence, self.name)
            for sink in self.sinks:
                try:
                    sink(event, crop)
                except Exception:
                    log.exception("Event sink failed")

    # ------------------------------------------------------------------ video out
    def _annotate(self, frame: np.ndarray, faces: list[DetectedFace]) -> np.ndarray:
        # Big main-stream frames (2K/4K) are shrunk for the browser: far less to encode and send.
        k = min(1.0, STREAM_MAX_WIDTH / frame.shape[1])
        if k < 1:
            img = cv2.resize(frame, (STREAM_MAX_WIDTH, int(frame.shape[0] * k)), interpolation=cv2.INTER_AREA)
            faces = [replace(f, bbox=tuple(int(v * k) for v in f.bbox)) for f in faces]
        else:
            img = frame.copy()
        scale = max(img.shape[1] / 1280, 0.5)
        thick = max(1, int(2 * scale))
        for f in faces:
            x1, y1, x2, y2 = f.bbox
            known = f.person_id is not None
            color = GREY if f.pending else GREEN if known else RED
            label = "..." if f.pending else f"{f.name} {f.confidence * 100:.0f}%" if known else "Unknown"
            cv2.rectangle(img, (x1, y1), (x2, y2), color, max(2, int(2 * scale)))
            (tw, th), base = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6 * scale, thick)
            ty = max(y1, th + base + 6)
            cv2.rectangle(img, (x1, ty - th - base - 6), (x1 + tw + 8, ty), color, -1)
            cv2.putText(img, label, (x1 + 4, ty - base - 2), cv2.FONT_HERSHEY_SIMPLEX, 0.6 * scale,
                        (255, 255, 255), thick, cv2.LINE_AA)
        stamp = f"{self.name}  {datetime.now():%Y-%m-%d %H:%M:%S}"
        cv2.putText(img, stamp, (12, img.shape[0] - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.6 * scale,
                    (0, 0, 0), thick + 2, cv2.LINE_AA)
        cv2.putText(img, stamp, (12, img.shape[0] - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.6 * scale,
                    (255, 255, 255), thick, cv2.LINE_AA)
        return img

    def _placeholder(self) -> np.ndarray:
        img = np.full((360, 640, 3), 24, dtype=np.uint8)
        msg = self.camera.error or "Connecting to camera..."
        cv2.putText(img, "NO SIGNAL", (230, 160), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (200, 200, 200), 2, cv2.LINE_AA)
        cv2.putText(img, self.name[:40], (20, 205), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (180, 180, 180), 1, cv2.LINE_AA)
        cv2.putText(img, msg[:70], (20, 235), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (150, 150, 150), 1, cv2.LINE_AA)
        return img

    def get_jpeg(self) -> bytes:
        """Annotated JPEG of the latest frame, encoded once and shared by all viewers."""
        frame_id, frame, _ = self.camera.read()
        with self._faces_lock:
            fresh = time.time() - self._faces_time < OVERLAY_TTL
            faces = self._faces if fresh else []
            faces_key = id(self._faces) if fresh else 0
        if frame is None or not self.camera.connected:
            key = ("placeholder", self.camera.error, int(time.time()))
        else:
            key = (frame_id, faces_key, int(time.time()))  # second changes the timestamp overlay
        with self._jpeg_lock:
            if self._jpeg_cache and self._jpeg_cache[0] == key:
                return self._jpeg_cache[1]
            img = self._placeholder() if key[0] == "placeholder" else self._annotate(frame, faces)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, self.cfg.stream_quality])
            data = buf.tobytes() if ok else b""
            self._jpeg_cache = (key, data)
            return data


class CameraManager:
    """Starts/stops one CameraWorker per enabled camera in the database."""

    def __init__(self, cfg: Settings, db: Database, engine: FaceEngine, index: FaceIndex, hub: Hub,
                 sinks: list[EventSink] | None = None, on_status: Callable[[dict], None] | None = None):
        self.cfg, self.db, self.engine, self.index, self.hub = cfg, db, engine, index, hub
        self.sinks: list[EventSink] = [self._publish_detection] + list(sinks or [])
        self.on_status = on_status
        self.extra_status: Callable[[], dict] | None = None  # e.g. Firebase sync state
        self.workers: dict[int, CameraWorker] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._status_thread: threading.Thread | None = None

    def _publish_detection(self, event: dict, crop: np.ndarray) -> None:
        self.hub.publish({"type": "detection", "event": event_to_json(event)})

    def seed_default_camera(self) -> None:
        """First run only: create 'Camera 1' from CAMERA_SOURCE so things work out of the box."""
        if self.db.get_meta("cameras_seeded"):
            return
        if not self.db.list_cameras():
            self.db.add_camera("Camera 1", self.cfg.camera_source)
        self.db.set_meta("cameras_seeded", "1")

    def start(self) -> None:
        self.seed_default_camera()
        for cam in self.db.list_cameras():
            if cam["enabled"]:
                self._start_worker(cam)
        self._stop.clear()
        self._status_thread = threading.Thread(target=self._status_loop, name="status", daemon=True)
        self._status_thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for w in list(self.workers.values()):
                w.stop()
            self.workers.clear()

    def _start_worker(self, cam: dict) -> None:
        w = CameraWorker(cam["id"], cam["name"], cam["source"], self.cfg, self.db, self.engine, self.index, self.sinks)
        with self._lock:
            self.workers[cam["id"]] = w
        w.start()

    def reload(self, camera_id: int) -> None:
        """Apply a camera's saved settings (after add / edit / enable / disable / delete)."""
        with self._lock:
            old = self.workers.pop(camera_id, None)
        if old:
            threading.Thread(target=old.stop, daemon=True).start()  # don't block the request on a slow camera
        cam = self.db.get_camera(camera_id)
        if cam and cam["enabled"]:
            self._start_worker(cam)

    def get(self, camera_id: int) -> CameraWorker | None:
        with self._lock:
            return self.workers.get(camera_id)

    def first(self) -> CameraWorker | None:
        with self._lock:
            return next(iter(self.workers.values()), None)

    def status(self) -> dict:
        with self._lock:
            workers = {w.id: w for w in self.workers.values()}
        cams = []
        for cam in self.db.list_cameras():
            w = workers.get(cam["id"])
            if w:
                cams.append({**w.status(), "enabled": True})
            else:
                cams.append({"id": cam["id"], "name": cam["name"], "source": describe_source(cam["source"]),
                             "enabled": False, "connected": False, "error": "Disabled", "capture_fps": 0,
                             "processing": {}})
        out = {
            "cameras": cams,
            "online": sum(1 for c in cams if c["connected"]),
            "known_embeddings": self.index.size,
            "clients": self.hub.client_count,
        }
        if self.extra_status:
            out.update(self.extra_status())
        return out

    def _status_loop(self) -> None:
        while not self._stop.wait(2):
            try:
                st = self.status()
                self.hub.publish({"type": "status", "status": st})
                if self.on_status:
                    self.on_status(st)
            except Exception:
                log.exception("Status update failed")
