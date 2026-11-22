@echo off
rem Start SafeFace automatically (minimised) every time you log in to Windows.
set "TARGET=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\SafeFace.cmd"
(
  echo @echo off
  echo start "SafeFace" /min cmd /c ""%~dp0run.bat" /background"
) > "%TARGET%"
if exist "%TARGET%" (
  echo.
  echo  Done. SafeFace will start by itself, minimised, when you log in to Windows.
  echo  To undo, run remove-autostart.bat.
  echo.
  echo  Tip: so it also runs after a power cut, set Windows to log in automatically
  echo  and set the PC's BIOS to "Power on after AC loss" / "Restore on AC power loss".
) else (
  echo  Could not create %TARGET%
)
echo.
pause
