@echo off
setlocal
title SafeFace
cd /d "%~dp0backend"

echo.
echo  ==============================
echo    SafeFace - starting up
echo  ==============================
echo.

rem ---- 1. Find a SUPPORTED Python -------------------------------------------
rem The AI engine (onnxruntime) has Windows builds for Python 3.10-3.14, 64-bit.
rem Newer Pythons (3.15+) are skipped: they can stay installed side by side.
set "PYCHECK=import sys,struct; sys.exit(0 if (3,10)<=sys.version_info[:2]<=(3,14) and struct.calcsize('P')==8 else 1)"
set "PY="
for %%V in (3.12 3.11 3.13 3.14 3.10) do if not defined PY (set "CAND=py -%%V" & call :trycand)
if not defined PY (set "CAND=python" & call :trycand)
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (set "CAND="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"" & call :trycand)
if not defined PY goto :installpython

:havepython
for /f "delims=" %%v in ('call %PY% -c "import sys; print(sys.version.split()[0])"') do set "PYVER=%%v"
echo  Using Python %PYVER%

rem ---- 2. First run: create the environment and install packages -----------
if exist ".venv\installed.ok" goto :run
rem A setup left over from an unsupported Python (e.g. 3.15) can never work: rebuild it.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "%PYCHECK%" >nul 2>&1 || (
    echo  Removing an old setup made with an unsupported Python version...
    rmdir /s /q ".venv"
  )
)
echo.
echo  First run: installing SafeFace. This takes 5-10 minutes...
echo.
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv
if not exist ".venv\Scripts\python.exe" goto :venvfail
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :installfail
echo ok> ".venv\installed.ok"

rem ---- 3. Start the server --------------------------------------------------
:run
echo.
echo  Starting... when you see "SafeFace ready." open  http://localhost:8000
echo  Keep this window open. Close it or press Ctrl+C to stop SafeFace.
echo  The first start downloads the face model, about 280 MB.
echo.
if /i not "%~1"=="/background" start "" /min powershell -NoProfile -WindowStyle Hidden -Command "for($i=0;$i -lt 150;$i++){try{Invoke-WebRequest -UseBasicParsing http://localhost:8000/api/health | Out-Null; Start-Process 'http://localhost:8000'; break}catch{Start-Sleep 2}}"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
echo.
echo  SafeFace has stopped. If you see an error above, copy the last lines and ask for help.
pause
exit /b 0

rem ---- helpers ---------------------------------------------------------------
:trycand
%CAND% -c "%PYCHECK%" >nul 2>&1 && set "PY=%CAND%"
exit /b 0

:installpython
echo  No supported Python found. SafeFace needs Python 3.10 to 3.14, 64-bit.
for /f "delims=" %%v in ('python --version 2^>nul') do echo  This PC has %%v, which is too new for the AI engine. It can stay installed.
where winget >nul 2>&1
if errorlevel 1 goto :nopython
echo.
choice /c YN /m "  Install Python 3.12 next to it automatically now (about 30 MB)"
if errorlevel 2 goto :nopython
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
  set "PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""
  goto :havepython
)
echo.
echo  Python was installed, but this window can't see it yet.
echo  Close this window and double-click run.bat again.
pause
exit /b 1

:nopython
echo.
echo  [ERROR] No supported Python found.
echo.
echo  How to fix:
echo    1. Download Python 3.12 "Windows installer (64-bit)" from
echo       https://www.python.org/downloads/release/python-31210/
echo       Not the newest version: SafeFace supports Python 3.10 to 3.14.
echo       It can be installed next to a newer Python.
echo    2. On the FIRST installer screen, tick  "Add python.exe to PATH",
echo       then click "Install Now".
echo    3. Close this window and double-click run.bat again.
echo.
echo  If typing "python" opens the Microsoft Store:
echo    Settings - Apps - Advanced app settings - App execution aliases,
echo    turn OFF "python.exe" and "python3.exe", then install Python as above.
echo.
pause
exit /b 1

:venvfail
echo.
echo  [ERROR] Could not create the Python environment in backend\.venv
echo  Try: delete the backend\.venv folder and run run.bat again.
echo.
pause
exit /b 1

:installfail
echo.
echo  [ERROR] Installing the Python packages failed. See the messages above.
echo.
echo  Most common causes:
echo    - No internet connection, or a firewall blocking pypi.org. Check and run again.
echo    - Unsupported Python. SafeFace needs Python 3.10 to 3.14, 64-bit.
echo  Double-click run.bat again: it continues where it stopped.
echo.
pause
exit /b 1
