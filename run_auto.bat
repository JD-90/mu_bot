@echo off
cd /d "%~dp0"

rem --- Python: install automatically if missing (official python.org build via winget, added to PATH) ---
call :findpy
if not defined PY (
  echo Python not found - installing Python 3.12 with winget...
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  call :findpy
)
if not defined PY (
  echo.
  echo Could not install Python automatically.
  echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH", then run this again.
  pause
  exit /b 1
)
echo Using %PY%

rem --- Dependencies (first run only) ---
if not exist ".deps_ok" (
  echo Installing dependencies - first run only...
  "%PY%" -m pip install --upgrade playwright || (pause & exit /b 1)
  "%PY%" -m playwright install chromium || (pause & exit /b 1)
  echo ok> .deps_ok
)

rem mu_supervisor.py runs the bot and restarts it on request (RESTART file / restart_bot.bat / R key),
rem after a crash or when it freezes. See supervisor.log.
"%PY%" mu_supervisor.py mu_auto_bot.py
if errorlevel 1 pause
exit /b 0

:findpy
set "PY="
for /f "delims=" %%P in ('where python 2^>nul') do (
  echo %%P | find /i "WindowsApps" >nul || if not defined PY set "PY=%%P"
)
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
exit /b 0
