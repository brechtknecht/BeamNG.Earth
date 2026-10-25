# Cold photoreal drive against a separate server (port 47700, an empty photogrammetry cache, a scratch user folder):
#   powershell -File tools\photoreal_bench.ps1 -Name base -From 48.1374,11.5755 -To 48.1620,11.6200 -Speed 25
param([string]$Name = "run", [string]$From = "48.1374,11.5755", [string]$To = "48.1620,11.6200",
      [double]$Speed = 25, [double]$Km = 4, [int]$Port = 47700, [switch]$Warm)
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
$work = Join-Path $env:TEMP "bnge_bench"
$out = Join-Path $work $Name
New-Item -ItemType Directory -Force $out | Out-Null
$user = Join-Path $work "user"
New-Item -ItemType Directory -Force $user | Out-Null
if (-not $Warm) {
    $cache = Join-Path $work "photoreal_$Name"
    if (Test-Path $cache) { Remove-Item -Recurse -Force $cache }
    $env:BNGE_PHOTOREAL_CACHE_DIR = $cache
}
$env:CESIUM_ION_TOKEN = (Get-ItemProperty HKCU:\Environment -Name CESIUM_ION_TOKEN).CESIUM_ION_TOKEN
$env:BNGE_LOG_FILE = Join-Path $out "server.log"
$py = Join-Path $repo ".venv\Scripts\python.exe"
$srv = Start-Process $py -PassThru -WindowStyle Hidden -WorkingDirectory $repo -ArgumentList @(
    "server\earthd.py", "--port", $Port, "--user-dir", $user, "--deshadow", "full", "--airports", "on",
    "--sea", "on", "--trees", "on", "--buildings", "on", "--near-visual", "photoreal")
try {
    $t = Get-Date
    while (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -EA SilentlyContinue)) {
        if ($srv.HasExited) { throw "server exited ($($srv.ExitCode)); see $out\server.log" }
        if (((Get-Date) - $t).TotalSeconds -gt 120) { throw "server not listening after 120 s" }
        Start-Sleep -Milliseconds 500
    }
    & $py (Join-Path $repo "tools\photoreal_drive.py") --port $Port --from $From --to $To --speed $Speed --km $Km --out $out
} finally {
    taskkill /T /F /PID $srv.Id 2>$null | Out-Null     # the server and its worker processes
}
