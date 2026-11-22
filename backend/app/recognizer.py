"""InsightFace wrapper (face detection + 512-d ArcFace embeddings) and vector matcher."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

import cv2
import numpy as np

log = logging.getLogger(__name__)


@dataclass
class DetectedFace:
    bbox: tuple[int, int, int, int]  # x1, y1, x2, y2 (pixels)
    det_score: float
    embedding: np.ndarray  # L2-normalised float32 (512,)
    name: str = "Unknown"
    person_id: int | None = None
    confidence: float = 0.0  # cosine similarity to best match


class FaceEngine:
    """Loads the InsightFace model pack once and exposes detect+embed."""

    def __init__(self, model_name: str, det_size: int, det_threshold: float, providers: list[str]):
        # Imported lazily so the rest of the app (and its tests) can load without the model.
        import onnxruntime as ort
        from insightface.app import FaceAnalysis

        available = set(ort.get_available_providers())
        chosen = [p for p in providers if p in available] or ["CPUExecutionProvider"]
        log.info("Loading InsightFace '%s' with providers %s", model_name, chosen)

        self._app = FaceAnalysis(
            name=model_name,
            providers=chosen,
            allowed_modules=["detection", "recognition"],  # skip age/gender/landmark models
        )
        ctx_id = 0 if "CUDAExecutionProvider" in chosen else -1
        self._app.prepare(ctx_id=ctx_id, det_thresh=det_threshold, det_size=(det_size, det_size))
        self._lock = threading.Lock()  # onnxruntime sessions are safe, but keep calls serialised

    def detect(self, bgr: np.ndarray, min_face_size: int = 0) -> list[DetectedFace]:
        with self._lock:
            faces = self._app.get(bgr)
        out: list[DetectedFace] = []
        h, w = bgr.shape[:2]
        for f in faces:
            x1, y1, x2, y2 = (int(round(v)) for v in f.bbox)
            x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
            if min(x2 - x1, y2 - y1) < min_face_size:
                continue
            emb = getattr(f, "normed_embedding", None)
            if emb is None:
                continue
            out.append(DetectedFace((x1, y1, x2, y2), float(f.det_score), emb.astype(np.float32)))
        return out

    def largest_face_embedding(self, bgr: np.ndarray) -> tuple[np.ndarray, tuple[int, int, int, int]] | None:
        """For enrolment: return the embedding of the biggest face in a photo."""
        faces = self.detect(bgr)
        offset = 0
        if not faces:
            # Tightly cropped headshots often fail detection: pad with a border and retry.
            offset = max(bgr.shape[:2]) // 2
            padded = cv2.copyMakeBorder(bgr, offset, offset, offset, offset, cv2.BORDER_CONSTANT, value=(0, 0, 0))
            faces = self.detect(padded)
        if not faces:
            return None
        best = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        h, w = bgr.shape[:2]
        x1, y1, x2, y2 = best.bbox
        bbox = (max(0, x1 - offset), max(0, y1 - offset), min(w, x2 - offset), min(h, y2 - offset))
        return best.embedding, bbox


class FaceIndex:
    """In-memory vector index. Brute-force cosine similarity is a single matrix
    multiply and handles thousands of embeddings in well under a millisecond.
    (Swap for FAISS/hnswlib if you ever enrol 100k+ faces.)"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._matrix = np.zeros((0, 512), dtype=np.float32)
        self._ids: list[int] = []
        self._names: list[str] = []

    def load(self, rows: list[tuple[int, str, np.ndarray]]) -> None:
        with self._lock:
            if rows:
                self._matrix = np.vstack([r[2] for r in rows]).astype(np.float32)
                self._ids = [r[0] for r in rows]
                self._names = [r[1] for r in rows]
            else:
                self._matrix = np.zeros((0, 512), dtype=np.float32)
                self._ids, self._names = [], []
        log.info("Face index loaded: %d embeddings, %d people", len(self._ids), len(set(self._ids)))

    @property
    def size(self) -> int:
        return len(self._ids)

    def match(self, embedding: np.ndarray, threshold: float) -> tuple[int | None, str, float]:
        with self._lock:
            if not self._ids:
                return None, "Unknown", 0.0
            sims = self._matrix @ embedding  # both L2-normalised -> cosine similarity
            i = int(np.argmax(sims))
            score = float(sims[i])
            if score >= threshold:
                return self._ids[i], self._names[i], score
            return None, "Unknown", score
