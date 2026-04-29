# MixedRelay v0.0.3 server build (PowerShell)
# Compiles ./cmd/mrelayd to mrelayd.exe at the repo root.
# Usage:
#   .\build-mrelayd.ps1
#   .\build-mrelayd.ps1 -GoExe C:\Go\bin\go.exe

[CmdletBinding()]
param(
    [string]$GoExe = $env:MRELAY_GO,
    [string]$Out   = "mrelayd.exe"
)

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

    Write-Host "[build] root = $Root"
    Write-Host "[build] go   = $GoExe"
    Write-Host "[build] out  = $(Join-Path $Root $Out)"
    Write-Host ""

    & $GoExe build -o $Out ./cmd/mrelayd
    if ($LASTEXITCODE -eq 0) { Write-Host "[build] OK" }
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
