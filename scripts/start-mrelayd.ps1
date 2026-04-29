# MixedRelay v0.0.3 server launcher (PowerShell) — localhost only
# Usage:
#   .\start-mrelayd.ps1
#   .\start-mrelayd.ps1 -Listen 127.0.0.1:6767
#   .\start-mrelayd.ps1 -Build   # build to mrelayd.exe and run that
#
# Default binds to 127.0.0.1:6767 (loopback only). For LAN access,
# use .\start-mrelayd_shared.ps1 instead.
#
# All wire I/O is echoed to stdout. Channel logs / cursors / archives live
# under -Data (default: data/ next to the repo root).

[CmdletBinding()]
param(
    [string]$Listen = $env:MRELAY_LISTEN,
    [string]$Data   = $env:MRELAY_DATA,
    [string]$GoExe  = $env:MRELAY_GO,
    [switch]$Build
)

if ([string]::IsNullOrEmpty($Listen)) { $Listen = "127.0.0.1:6767" }
if ([string]::IsNullOrEmpty($Data))   { $Data   = "data" }
if ([string]::IsNullOrEmpty($GoExe)) {
    $cmd = Get-Command go -ErrorAction SilentlyContinue
    if ($cmd) { $GoExe = $cmd.Source }
}

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Push-Location $Root
try {
    if ([string]::IsNullOrEmpty($GoExe)) {
        Write-Error "go not found in PATH. Install Go and add it to PATH, or set -GoExe / `$env:MRELAY_GO."
        exit 1
    }
    if (-not (Test-Path $GoExe)) {
        Write-Error "go.exe not found at '$GoExe'. Set -GoExe or `$env:MRELAY_GO."
        exit 1
    }

    Write-Host "[mrelayd] root = $Root"
    Write-Host "[mrelayd] go   = $GoExe"
    Write-Host "[mrelayd] addr = $Listen"
    Write-Host "[mrelayd] data = $Data"
    Write-Host ""

    if ($Build) {
        & $GoExe build -o mrelayd.exe ./cmd/mrelayd
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & .\mrelayd.exe --addr $Listen --data $Data
    } else {
        & $GoExe run ./cmd/mrelayd --addr $Listen --data $Data
    }
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
