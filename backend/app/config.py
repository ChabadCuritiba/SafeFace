"""Runtime configuration, read from environment variables (or a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional: load backend/.env if python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # pragma: no cover
    pass


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


BASE_DIR = Path(__file__).resolve().parent.parent  # backend/


@dataclass
class Settings:
    # --- Camera -------------------------------------------------------------
    # "0" (or any integer) = local USB webcam index.
    # "rtsp://user:pass@192.168.1.50:554/stream1" = IP camera.
    # A path to a video file also works (handy for demos).
    camera_source: str = os.getenv("CAMERA_SOURCE", "0")
    camera_width: int = int(os.getenv("CAMERA_WIDTH", "1280"))
    camera_height: int = int(os.getenv("CAMERA_HEIGHT", "720"))

    # --- Processing ---------------------------------------------------------
    process_fps: float = float(os.getenv("PROCESS_FPS", "3"))  # recognition passes/sec
    stream_fps: float = float(os.getenv("STREAM_FPS", "12"))  # MJPEG frames/sec to browser
    stream_quality: int = int(os.getenv("STREAM_JPEG_QUALITY", "70"))
    motion_enabled: bool = _bool("MOTION_ENABLED", True)
    motion_threshold: float = float(os.getenv("MOTION_THRESHOLD", "0.004"))  # fraction of pixels changed
    # keep running recognition this many seconds after the last motion / face
    motion_hold_seconds: float = float(os.getenv("MOTION_HOLD_SECONDS", "3"))

    # --- Recognition --------------------------------------------------------
    model_name: str = os.getenv("MODEL_NAME", "buffalo_l")  # buffalo_l (accurate) | buffalo_s (fast)
    det_size: int = int(os.getenv("DET_SIZE", "640"))
    det_threshold: float = float(os.getenv("DET_THRESHOLD", "0.5"))
    match_threshold: float = float(os.getenv("MATCH_THRESHOLD", "0.45"))  # cosine similarity
    min_face_size: int = int(os.getenv("MIN_FACE_SIZE", "40"))  # px, ignore tiny faces
    providers: list[str] = field(
        default_factory=lambda: _list("ONNX_PROVIDERS", "CUDAExecutionProvider,CPUExecutionProvider")
    )

    # --- Events -------------------------------------------------------------
    event_cooldown_seconds: float = float(os.getenv("EVENT_COOLDOWN_SECONDS", "30"))
    log_unknown: bool = _bool("LOG_UNKNOWN", False)

    # --- Storage ------------------------------------------------------------
    data_dir: Path = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))

    # --- Web ----------------------------------------------------------------
    # If set, every /api, /media and /ws request must carry this key
    # (header "X-API-Key" or query "?key="). STRONGLY recommended when the
    # backend is reachable from the internet (e.g. behind Firebase Hosting).
    api_key: str = os.getenv("API_KEY", "")
    cors_origins: list[str] = field(default_factory=lambda: _list("CORS_ORIGINS", "*"))
    serve_frontend: bool = _bool("SERVE_FRONTEND", True)
    frontend_dir: Path = Path(os.getenv("FRONTEND_DIR", str(BASE_DIR / "dashboard")))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "safeface.db"

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def faces_dir(self) -> Path:
        return self.data_dir / "faces"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.snapshots_dir, self.faces_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
