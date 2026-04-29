# MixedRelay v0.0.3 server launcher (PowerShell) — LAN/shared mode
# Usage:
#   .\start-mrelayd_shared.ps1
#   .\start-mrelayd_shared.ps1 -Listen 0.0.0.0:6767
#   .\start-mrelayd_shared.ps1 -Build   # build to mrelayd.exe and run that
#
# Binds to 0.0.0.0:6767 by default so other hosts on the LAN can connect.
#
# SECURITY: MixedRelay has NO authentication and NO encryption.
# Anyone on the network can join channels and read all messages.
# Only run this on a trusted LAN. Never put secrets on the relay.
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

if ([string]::IsNullOrEmpty($Listen)) { $Listen = "0.0.0.0:6767" }
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

    Write-Warning "LAN/shared mode -- listening on $Listen"
    Write-Warning "No authentication, no encryption. Anyone on this network can read everything."
    Write-Warning "Only use on a trusted LAN. Never put secrets on this relay."
    Write-Host ""
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
