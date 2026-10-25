@echo off
rem Personal fork: BeamNG.Earth with the photorealistic near ground (Google Photorealistic 3D Tiles via Cesium ion).
rem Needs your Cesium ion token: setx CESIUM_ION_TOKEN "eyJ..." once in a command prompt, then open a new one.
rem The physics stays the graded terrain; the photogrammetry is drawn only. Compute sharing is off in this mode.
rem The downloaded photogrammetry is capped at 3 GB (least recently used out first, ~1 GB per km driven);
rem another cap: setx BNGE_PHOTOREAL_CACHE_GB 5
if not "%CESIUM_ION_TOKEN%%GOOGLE_MAPS_API_KEY%"=="" goto run
echo CESIUM_ION_TOKEN is not set. Run:  setx CESIUM_ION_TOKEN "your token from ion.cesium.com/tokens"
echo then open a new window and double-click this file again.
pause
exit /b 1
:run
rem the server's log also goes to photoreal.log next to this file (to send after a drive)
set "BNGE_LOG_FILE=%~dp0photoreal.log"
rem --deshadow off: the shadow removal renders the far tiles' photos in the server process itself (start.py turns
rem it on); at speed it held up the photo tiles (simulated drive at 100 km/h: near tiles 5 s late with it, 0 s without)
call "%~dp0run_server.bat" -- --near-visual photoreal --deshadow off %*
