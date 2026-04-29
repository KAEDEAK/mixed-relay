# Stop MixedRelay server (PowerShell).
# Usage:
#   .\stop-mrelayd.ps1
#   .\stop-mrelayd.ps1 -Port 6767

[CmdletBinding()]
param(
    [int]$Port = $(if ($env:MRELAY_PORT) { [int]$env:MRELAY_PORT } else { 6767 })
)

$conns = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if (-not $conns) {
    Write-Host "[stop-mrelayd] no listener on port $Port"
    exit 1
}

$pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($procId in $pids) {
    try {
        $p = Get-Process -Id $procId -ErrorAction Stop
        Write-Host "[stop-mrelayd] killing PID $procId ($($p.ProcessName)) listening on :$Port"
        Stop-Process -Id $procId -Force -ErrorAction Stop
        Write-Host "[stop-mrelayd] PID $procId terminated"
    } catch {
        Write-Warning "[stop-mrelayd] failed to kill PID $procId : $_"
    }
}
exit 0
