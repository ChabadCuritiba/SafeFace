@echo off
REM Start SafeFace locally on Windows, then open http://localhost:8000
cd /d "%~dp0backend"
if not exist .venv (
  py -3.11 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\pip install -r requirements.txt
)
.venv\Scripts\uvicorn app.main:app --host 0.0.0.0 --port 8000
