# Container for the SafeFace backend (local Docker, a mini-PC, or Google Cloud Run).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# build-essential is needed to compile insightface; libgl/glib are OpenCV runtime deps (the wheel bundles FFmpeg for RTSP).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake the face models into the image so cold starts don't download 280 MB.
ARG MODEL_NAME=buffalo_l
ENV MODEL_NAME=${MODEL_NAME} INSIGHTFACE_HOME=/root/.insightface
RUN python -c "from insightface.app import FaceAnalysis as F; \
F(name='${MODEL_NAME}', providers=['CPUExecutionProvider'], allowed_modules=['detection','recognition']).prepare(ctx_id=-1)"

COPY app ./app

# The frontend is hosted on Firebase, so the API container doesn't serve it.
ENV SERVE_FRONTEND=false DATA_DIR=/data PORT=8080
VOLUME ["/data"]
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
