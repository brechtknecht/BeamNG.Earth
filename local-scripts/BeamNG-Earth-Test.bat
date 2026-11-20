@echo off
title BeamNG.Earth Test
rem Double-click: latest version, server with 3 cores, then the game. Does not touch your cache or settings.
rem The server's log also goes to C:\Users\felix\dev\server-<date>-<time>.log.
set "REPO=C:\Users\felix\dev\BeamNG.Earth"
set "GAME=D:\SteamLibrary\steamapps\common\BeamNG.drive"
set BNGE_SERVER_CORES=3
set BNGE_SQUARE=2

netstat -ano | findstr /r /c:"127\.0\.0\.1:47613  *0\.0\.0\.0:0" >nul
if not errorlevel 1 (
  echo Der Server laeuft schon. Ich lasse ihn so.
  goto game
)

echo Hole den neuesten Stand ...
git -C "%REPO%" pull --ff-only origin claude/merge-181
if errorlevel 1 echo Konnte nicht aktualisieren, starte den vorhandenen Stand.

echo Starte den Server mit 3 Kernen ...
for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%t"
set "SLOG=C:\Users\felix\dev\server-%STAMP%.log"
set PYTHONUNBUFFERED=1
echo Server-Log: %SLOG%
start "BeamNG.Earth Server" /d "%REPO%" powershell -NoProfile -NoExit -ExecutionPolicy Bypass -File C:\Users\felix\dev\bnge-tee.ps1

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
echo Fertig. Im Spiel:
echo   1. Freeroam, Level BeamNG.Earth
echo   2. Zu einem Ort teleportieren, an dem du noch nie warst
echo   3. Warten, bis die Ladeanzeige bei etwa 90 %% ist
echo   4. Los
echo.
pause
