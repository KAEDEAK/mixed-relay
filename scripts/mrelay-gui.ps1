# MixedRelay GUI client launcher (PowerShell).
[CmdletBinding()]
param(
    [string]$Addr = $env:MRELAY_ADDR,
    [string]$Nick = $env:MRELAY_NICK,
    [string]$Kind = $env:MRELAY_KIND,
    [string]$Py   = $env:MRELAY_PY
)

if ([string]::IsNullOrEmpty($Addr)) { $Addr = "127.0.0.1:6767" }
if ([string]::IsNullOrEmpty($Nick)) { $Nick = "alice" }
if ([string]::IsNullOrEmpty($Kind)) { $Kind = "human" }
if ([string]::IsNullOrEmpty($Py))   { $Py   = "python" }

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Push-Location $Root
try {
    $env:MRELAY_ADDR = $Addr
    $env:MRELAY_NICK = $Nick
    $env:MRELAY_KIND = $Kind

    Write-Host "[mrelay-gui] root = $Root"
    Write-Host "[mrelay-gui] addr = $Addr"
    Write-Host "[mrelay-gui] nick = $Nick"
    Write-Host ""

    & $Py -m mrelay_gui
    $rc = $LASTEXITCODE
    if (-not $env:MRELAY_NOPAUSE) {
        Read-Host "Press Enter to close"
    }
    exit $rc
}
finally {
    Pop-Location
}
