"""Optional: mirror detections and camera status to Firebase so the SafeFace
website (Firebase Hosting) shows the PC's alerts from anywhere.

Turned on when a Firebase service-account key file exists
(FIREBASE_CREDENTIALS, default backend/firebase-key.json).
All network calls happen on a background thread, so a slow or offline
internet connection never slows down the cameras.
"""
from __future__ import annotations

import base64
import logging
import queue
import threading
import time
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 120  # status doc refresh (keeps well inside the free plan's write quota)


def thumb_data_url(img: np.ndarray, max_side: int = 160) -> str:
    h, w = img.shape[:2]
    s = min(1.0, max_side / max(h, w, 1))
    if s < 1:
        img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


class CloudSync:
    def __init__(self, credentials_path: Path):
        import firebase_admin
        from firebase_admin import credentials, firestore

        cred = credentials.Certificate(str(credentials_path))
        self.app = firebase_admin.initialize_app(cred, name="safeface")
        self.db = firestore.client(self.app)
        self.project_id = cred.project_id
        self._q: queue.Queue = queue.Queue(maxsize=500)
        self._last_status_key = None
        self._last_status_at = 0.0
        self.connected = True
        self.last_error: str | None = None
        threading.Thread(target=self._worker, name="cloud-sync", daemon=True).start()
        log.info("Firebase sync on (project %s)", self.project_id)

    # ---- producers (called from camera threads) ------------------------------
    def push_event(self, event: dict, crop: np.ndarray) -> None:
        doc = {
            "name": event["name"],
            "personId": None,  # PC faces are a separate database from the website's
            "confidence": float(event["confidence"]),
            "snapshot": thumb_data_url(crop),
            "camera": event.get("camera") or "PC camera",
            "timestamp": int(event["timestamp"] * 1000),
            "source": "pc",
        }
        self._put(("event", doc))

    def push_status(self, status: dict) -> None:
        cams = [{"name": c["name"], "connected": bool(c["connected"]), "enabled": bool(c.get("enabled", True))}
                for c in status.get("cameras", [])]
        key = tuple((c["name"], c["connected"], c["enabled"]) for c in cams)
        now = time.time()
        if key == self._last_status_key and now - self._last_status_at < HEARTBEAT_SECONDS:
            return  # nothing changed and heartbeat not due
        self._last_status_key, self._last_status_at = key, now
        self._put(("status", {"online": True, "updatedAt": int(now * 1000), "cameras": cams}))

    def mark_offline(self) -> None:
        """Best effort on shutdown, so the website shows 'PC offline' straight away."""
        try:
            self.db.collection("system").document("pc").set(
                {"online": False, "updatedAt": int(time.time() * 1000)}, merge=True, timeout=5)
        except Exception:
            pass

    def _put(self, item) -> None:
        try:
            self._q.put_nowait(item)
        except queue.Full:
            log.warning("Firebase sync queue full - dropping an update (internet down?)")

    # ---- consumer --------------------------------------------------------------
    def _worker(self) -> None:
        backoff = 2.0
        while True:
            kind, doc = self._q.get()
            while True:
                try:
                    if kind == "event":
                        self.db.collection("events").add(doc, timeout=15)
                    else:
                        self.db.collection("system").document("pc").set(doc, timeout=15)
                    if not self.connected:
                        log.info("Firebase sync reconnected")
                    self.connected, self.last_error, backoff = True, None, 2.0
                    break
                except Exception as e:  # network down, quota, bad key...
                    self.connected, self.last_error = False, str(e)[:200]
                    log.warning("Firebase sync failed (%s) - retrying in %.0fs", type(e).__name__, backoff)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 300)


def start_cloud(credentials_path: Path) -> CloudSync | None:
    if not credentials_path.is_file():
        log.info("Firebase sync off (no key file at %s)", credentials_path)
        return None
    try:
        return CloudSync(credentials_path)
    except ImportError:
        log.warning("Firebase key found but firebase-admin isn't installed: pip install firebase-admin")
    except Exception as e:
        log.error("Firebase sync disabled - couldn't read %s: %s", credentials_path.name, e)
    return None
