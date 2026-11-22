"""Threaded video capture for USB webcams, RTSP streams and video files.

A background thread reads frames as fast as the source delivers them and keeps
ONLY the latest one. This is important for RTSP: if you read slower than the
camera sends, OpenCV's internal buffer fills up and you end up processing
footage that is several seconds old.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import cv2
import numpy as np

log = logging.getLogger(__name__)


def parse_source(source: str) -> int | str:
    source = str(source).strip()
    return int(source) if source.isdigit() else source


def describe_source(source: str) -> str:
    """Human-readable source name with any RTSP password hidden."""
    s = str(source)
    if s.isdigit():
        return f"USB webcam #{s}"
    if "://" in s and "@" in s:
        scheme, rest = s.split("://", 1)
        creds, host = rest.rsplit("@", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    if os.path.isfile(s):
        return f"file: {os.path.basename(s)}"
    return s


class Camera:
    def __init__(self, source: str, width: int = 0, height: int = 0):
        self.source = str(source)
        self.width = width
        self.height = height
        self._frame: np.ndarray | None = None
        self._frame_id = 0
        self._frame_time = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False
        self.error: str | None = None
        self.capture_fps = 0.0

    # ------------------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.connected = False

    def read(self) -> tuple[int, np.ndarray | None, float]:
        """Return (frame_id, frame, timestamp). frame_id increments per new frame."""
        with self._lock:
            return self._frame_id, self._frame, self._frame_time

    # ------------------------------------------------------------------
    def _open(self) -> cv2.VideoCapture:
        src = parse_source(self.source)
        if isinstance(src, str) and src.lower().startswith("rtsp"):
            # TCP is far more reliable than UDP over Wi-Fi / the internet.
            os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
            params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 10000]
            cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG, params)
        else:
            cap = cv2.VideoCapture(src)
        if isinstance(src, int):
            if self.width:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            if self.height:
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _run(self) -> None:
        backoff = 1.0
        src = parse_source(self.source)
        is_file = isinstance(src, str) and os.path.isfile(src)
        while not self._stop.is_set():
            cap = self._open()
            if not cap.isOpened():
                self.connected = False
                self.error = f"Cannot open {describe_source(self.source)}"
                log.warning("%s - retrying in %.0fs", self.error, backoff)
                cap.release()
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)
                continue

            log.info("Camera connected: %s", describe_source(self.source))
            self.connected, self.error, backoff = True, None, 1.0
            file_delay = 1.0 / (cap.get(cv2.CAP_PROP_FPS) or 25) if is_file else 0
            frames, t0 = 0, time.time()

            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    if is_file:  # loop video files for demos
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    self.error = "Stream interrupted"
                    log.warning("Camera stream lost, reconnecting...")
                    break
                with self._lock:
                    self._frame = frame
                    self._frame_id += 1
                    self._frame_time = time.time()
                frames += 1
                if time.time() - t0 >= 2:
                    self.capture_fps = frames / (time.time() - t0)
                    frames, t0 = 0, time.time()
                if file_delay:
                    time.sleep(file_delay)

            cap.release()
            self.connected = False
            self._stop.wait(1)
