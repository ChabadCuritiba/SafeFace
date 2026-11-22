@echo off
setlocal
title SafeFace
cd /d "%~dp0backend"

echo.
echo  ==============================
echo    SafeFace - starting up
echo  ==============================
echo.

rem ---- 1. Find Python (3.11 preferred) --------------------------------------
set "PY="
py -3.11 -c "import sys" >nul 2>&1 && set "PY=py -3.11"
if not defined PY py -3.12 -c "import sys" >nul 2>&1 && set "PY=py -3.12"
if not defined PY py -3.10 -c "import sys" >nul 2>&1 && set "PY=py -3.10"
if not defined PY py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>&1 && set "PY=python"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Python311\python.exe""
if not defined PY goto :installpython
:havepython
for /f "delims=" %%v in ('call %PY% -c "import sys; print(sys.version.split()[0])"') do set "PYVER=%%v"
echo  Using Python %PYVER%

rem ---- 2. First run: create the environment and install packages -----------
if exist ".venv\installed.ok" goto :run
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

:installpython
echo  Python is not installed on this PC.
where winget >nul 2>&1
if errorlevel 1 goto :nopython
echo.
choice /c YN /m "  Install Python 3.11 automatically now (about 30 MB)"
if errorlevel 2 goto :nopython
winget install -e --id Python.Python.3.11 --scope user --accept-package-agreements --accept-source-agreements
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
  set "PY="%LOCALAPPDATA%\Programs\Python\Python311\python.exe""
  goto :havepython
)
echo.
echo  Python was installed, but this window can't see it yet.
echo  Close this window and double-click run.bat again.
pause
exit /b 1

:nopython
echo  [ERROR] Python is not installed, or it is not on PATH.
echo.
echo  How to fix:
echo    1. Download Python 3.11 "Windows installer 64-bit" from
echo       https://www.python.org/downloads/release/python-3119/
echo    2. On the FIRST installer screen, tick  "Add python.exe to PATH",
echo       then click "Install Now".
echo    3. Close this window and double-click run.bat again.
echo.
echo  If Python is already installed and typing "python" opens the Microsoft Store:
echo    Settings - Apps - Advanced app settings - App execution aliases,
echo    turn OFF "python.exe" and "python3.exe", then reinstall Python as above.
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
echo    - Very new or very old Python. Python 3.11 or 3.12 is recommended.
echo  Double-click run.bat again: it continues where it stopped.
echo.
pause
exit /b 1
