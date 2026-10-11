@echo off
rem Restart the MU auto bot right away (double-click). If it isn't running, start it.
rem For a restart at the next safe time (:10-:45) create an empty file named RESTART instead.
cd /d "%~dp0"
netstat -ano | findstr /r /c:"127.0.0.1:47292 .*LISTENING" >nul
if errorlevel 1 (
  echo The bot is not running under the supervisor - starting run_auto.bat...
  start "MU auto bot" "%~dp0run_auto.bat"
) else (
  echo now> RESTART
  echo Restart requested - the bot closes and starts again in a few seconds. See supervisor.log.
)
timeout /t 4 >nul
