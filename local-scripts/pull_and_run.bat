@echo off
rem BeamNG.Earth test build: pulls the newest test branch (the combined speed fixes from the other threads + crop
rem fields), updates the server and the mod, and starts the server. Its log goes to C:\Users\felix\dev\server-run.log.
rem Double-click it, then start BeamNG.drive. Crops off:  pull_and_run.bat nocrops
setlocal
set REPO=%~dp0BeamNG.Earth
set BRANCH=claude/project-thread-ddh3j9
cd /d "%REPO%" || goto fail

git fetch origin %BRANCH% || goto fail
rem local edits (mostly line endings) are kept in a stash, not lost: "git stash list" shows them
git stash push -q -m "pull_and_run backup" >nul 2>nul
git checkout -q -B %BRANCH% origin/%BRANCH% || goto fail
echo Test build:
git log --oneline -1

set BNGE_CROPS=
if /i "%~1"=="nocrops" set BNGE_CROPS=0

rem the installer only changes what is out of date (new server packages, the mod)
if exist ".venv\Scripts\python.exe" (".venv\Scripts\python.exe" install.py --yes) else (py -3 install.py --yes || python install.py --yes)
if not exist ".venv\Scripts\python.exe" goto fail

powershell -NoProfile -Command "& '.venv\Scripts\python.exe' start.py 2>&1 | Tee-Object '%~dp0server-run.log'"
echo.
echo The server has stopped. Log: %~dp0server-run.log
pause
exit /b 0

:fail
echo.
echo Something failed (see above).
pause
exit /b 1
