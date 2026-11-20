@echo off
title BeamNG.Earth Upstream (TecnaGamer dev)
rem Double-click: TecnaGamer's latest dev (branch upstream-dev, none of our spike branches), server with 3 cores, then the game.
rem Does not touch your cache or settings. Server log also goes to C:\Users\felix\dev\server-<date>-<time>.log.
set "REPO=C:\Users\felix\dev\BeamNG.Earth"
set "GAME=D:\SteamLibrary\steamapps\common\BeamNG.drive"
set BNGE_SERVER_CORES=3

netstat -ano | findstr /r /c:"127\.0\.0\.1:47613  *0\.0\.0\.0:0" >nul
if not errorlevel 1 (
  echo Der Server laeuft schon. Ich lasse ihn so.
  goto game
)

echo Wechsle auf upstream-dev ...
git -C "%REPO%" checkout upstream-dev
if errorlevel 1 (
  echo Konnte nicht auf upstream-dev wechseln ^(lokale Aenderungen?^). Abbruch.
  pause
  exit /b 1
)
echo Hole den neuesten Stand von TecnaGamer/dev ...
git -C "%REPO%" fetch --no-tags https://github.com/TecnaGamer/BeamNG.Earth dev
git -C "%REPO%" merge --ff-only FETCH_HEAD
if errorlevel 1 echo Konnte nicht aktualisieren, starte den vorhandenen Stand.

echo Starte den Server mit 3 Kernen ...
for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%t"
set "SLOG=C:\Users\felix\dev\server-%STAMP%.log"
set PYTHONUNBUFFERED=1
echo Server-Log: %SLOG%
start "BeamNG.Earth Server (upstream)" /d "%REPO%" powershell -NoProfile -NoExit -ExecutionPolicy Bypass -File C:\Users\felix\dev\bnge-tee.ps1

:wait
timeout /t 2 /nobreak >nul
netstat -ano | findstr /r /c:"127\.0\.0\.1:47613  *0\.0\.0\.0:0" >nul
if errorlevel 1 goto wait

:game
tasklist /fi "imagename eq BeamNG.drive.x64.exe" | findstr /i "BeamNG.drive.x64.exe" >nul
if not errorlevel 1 (
  echo Das Spiel laeuft schon.
) else (
  echo Starte das Spiel ...
  start "" /d "%GAME%" "%GAME%\Bin64\BeamNG.drive.x64.exe"
)

echo.
echo Fertig. Im Spiel: Freeroam, Level BeamNG.Earth.
echo.
pause
