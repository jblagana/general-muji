# muji launcher helper (called by start.bat).
# 1. If our own server is already up on the configured port -> open it, exit 0.
# 2. Otherwise start server.py minimized, wait for it to answer /api/health
#    (it records its real port in server.port if it had to move), open the
#    browser, and stay in the foreground: Ctrl+C / closing the window stops
#    the server.
$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$port = [int]((Select-String -Path .env -Pattern 'PORT=' | Select-Object -First 1).Line -split '=', 2)[1].Trim()
if (-not $port) { $port = 8321 }

function Test-OurServer($p) {
    try {
        $r = Invoke-WebRequest -Uri ("http://127.0.0.1:" + $p + "/api/health") -UseBasicParsing -TimeoutSec 2
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

if (Test-OurServer $port) {
    Write-Host "Server is already running - opening the browser."
    Start-Process ("http://127.0.0.1:" + $port)
    exit 0
}

Write-Host "Starting server..."
$proc = Start-Process -FilePath ".venv\Scripts\python.exe" -ArgumentList "server.py" `
    -WorkingDirectory $root -WindowStyle Minimized -PassThru
try {
    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline) {
        if ($proc.HasExited) { break }
        $p = Get-Content "server.port" -ErrorAction SilentlyContinue
        $real = if ($p) { [int]$p.Trim() } else { $port }
        if (Test-OurServer $real) {
            Write-Host ("  muji  ->  http://127.0.0.1:" + $real)
            Start-Process ("http://127.0.0.1:" + $real)
            break
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not (Test-OurServer $port) -and -not (Get-Content "server.port" -ErrorAction SilentlyContinue)) {
        Write-Host "Server did not come up - check the minimized console window for errors."
        exit 1
    }
    Write-Host "Server running - close this window (or press Ctrl+C) to stop."
    $proc.WaitForExit()
} finally {
    if (-not $proc.HasExited) { $proc.Kill() }
    Remove-Item "server.port" -ErrorAction SilentlyContinue
}
Write-Host "Server stopped."
