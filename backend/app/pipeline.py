"""The real-time loop: camera -> motion gate -> detect -> match -> log/alert.

Two independent rates keep CPU usage low while the video still looks smooth:
  * recognition runs at PROCESS_FPS (default 3/s) and only while there is motion;
  * the browser stream is re-encoded at STREAM_FPS with the most recent
    recognition results drawn on top.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path

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
OVERLAY_TTL = 1.5  # seconds a box stays on screen after the last detection


def safe_filename(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")[:40] or "face"


def crop_face(frame: np.ndarray, bbox: tuple[int, int, int, int], margin: float = 0.35) -> np.ndarray:
    x1, y1, x2, y2 = bbox
    h, w = frame.shape[:2]
    mx, my = int((x2 - x1) * margin), int((y2 - y1) * margin)
    return frame[max(0, y1 - my): min(h, y2 + my), max(0, x1 - mx): min(w, x2 + mx)].copy()


def event_to_json(event: dict) -> dict:
    out = dict(event)
    out["snapshot_url"] = f"/media/{event['snapshot_path']}" if event.get("snapshot_path") else None
    return out


class Pipeline:
    def __init__(self, cfg: Settings, db: Database, engine: FaceEngine, index: FaceIndex, hub: Hub):
        self.cfg, self.db, self.engine, self.index, self.hub = cfg, db, engine, index, hub
        self.camera = Camera(cfg.camera_source, cfg.camera_width, cfg.camera_height)
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

        self.stats = {"process_fps": 0.0, "process_ms": 0.0, "motion": False, "active": False, "faces": 0}

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        self.camera.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="recognition", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.camera.stop()

    def switch_camera(self, source: str) -> None:
        log.info("Switching camera to %s", describe_source(source))
        old = self.camera
        self.camera = Camera(source, self.cfg.camera_width, self.cfg.camera_height)
        old.stop()
        self._bg = None
        with self._faces_lock:
            self._faces = []
        self.camera.start()

    def status(self) -> dict:
        return {
            "camera": {
                "source": describe_source(self.camera.source),
                "connected": self.camera.connected,
                "error": self.camera.error,
                "capture_fps": round(self.camera.capture_fps, 1),
            },
            "processing": {k: (round(v, 1) if isinstance(v, float) else v) for k, v in self.stats.items()},
            "known_embeddings": self.index.size,
            "clients": self.hub.client_count,
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
        interval = 1.0 / max(self.cfg.process_fps, 0.1)
        last_id, passes, t_stats, t_status = -1, 0, time.time(), 0.0
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

                if active:
                    t0 = time.time()
                    try:
                        faces = self.engine.detect(frame, self.cfg.min_face_size)
                    except Exception:  # never let one bad frame kill the loop
                        log.exception("Detection failed")
                        faces = []
                    for f in faces:
                        f.person_id, f.name, f.confidence = self.index.match(f.embedding, self.cfg.match_threshold)
                    self.stats["process_ms"] = (time.time() - t0) * 1000
                    self.stats["faces"] = len(faces)
                    passes += 1
                    if faces:
                        self._last_activity = time.time()
                        with self._faces_lock:
                            self._faces, self._faces_time = faces, time.time()
                        self._handle_events(frame, faces)

            now = time.time()
            if now - t_stats >= 2:
                self.stats["process_fps"] = passes / (now - t_stats)
                passes, t_stats = 0, now
            if now - t_status >= 2:
                self.hub.publish({"type": "status", "status": self.status()})
                t_status = now

            self._stop.wait(max(0.0, interval - (time.time() - loop_start)))

    def _handle_events(self, frame: np.ndarray, faces: list[DetectedFace]) -> None:
        now = time.time()
        for f in faces:
            if f.person_id is None and not self.cfg.log_unknown:
                continue
            key = f"p{f.person_id}" if f.person_id is not None else "unknown"
            if now - self._last_event.get(key, 0) < self.cfg.event_cooldown_seconds:
                continue
            self._last_event[key] = now

            day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
            rel = Path("snapshots") / day / f"{datetime.fromtimestamp(now):%H%M%S}_{int(now * 1000) % 1000:03d}_{safe_filename(f.name)}.jpg"
            (self.cfg.data_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(self.cfg.data_dir / rel), crop_face(frame, f.bbox), [cv2.IMWRITE_JPEG_QUALITY, 90])

            event = self.db.add_event(
                f.person_id, f.name, round(f.confidence, 4), rel.as_posix(),
                describe_source(self.camera.source), now,
            )
            log.info("Detected %s (%.2f)", f.name, f.confidence)
            self.hub.publish({"type": "detection", "event": event_to_json(event)})

    # ------------------------------------------------------------------ video out
    def _annotate(self, frame: np.ndarray, faces: list[DetectedFace]) -> np.ndarray:
        img = frame.copy()
        scale = max(img.shape[1] / 1280, 0.5)
        for f in faces:
            x1, y1, x2, y2 = f.bbox
            known = f.person_id is not None
            color = GREEN if known else RED
            label = f"{f.name} {f.confidence * 100:.0f}%" if known else "Unknown"
            cv2.rectangle(img, (x1, y1), (x2, y2), color, max(2, int(2 * scale)))
            (tw, th), base = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6 * scale, max(1, int(2 * scale)))
            ty = max(y1, th + base + 6)
            cv2.rectangle(img, (x1, ty - th - base - 6), (x1 + tw + 8, ty), color, -1)
            cv2.putText(img, label, (x1 + 4, ty - base - 2), cv2.FONT_HERSHEY_SIMPLEX, 0.6 * scale,
                        (255, 255, 255), max(1, int(2 * scale)), cv2.LINE_AA)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(img, stamp, (12, img.shape[0] - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.6 * scale,
                    (255, 255, 255), max(1, int(2 * scale)), cv2.LINE_AA)
        return img

    def _placeholder(self) -> np.ndarray:
        img = np.full((360, 640, 3), 24, dtype=np.uint8)
        msg = self.camera.error or "Connecting to camera..."
        cv2.putText(img, "NO SIGNAL", (230, 170), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (200, 200, 200), 2, cv2.LINE_AA)
        cv2.putText(img, msg[:60], (20, 215), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), 1, cv2.LINE_AA)
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
            key = (id(self.camera), frame_id, faces_key)
        with self._jpeg_lock:
            if self._jpeg_cache and self._jpeg_cache[0] == key:
                return self._jpeg_cache[1]
            img = self._placeholder() if key[0] == "placeholder" else self._annotate(frame, faces)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, self.cfg.stream_quality])
            data = buf.tobytes() if ok else b""
            self._jpeg_cache = (key, data)
            return data
