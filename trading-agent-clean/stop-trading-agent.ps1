param(
    [int]$BackendPort = 8011,
    [int]$FrontendPort = 5173
)

$ErrorActionPreference = "Stop"

function Get-PortProcessIds([int]$Port) {
    $pattern = "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$"
    @(& netstat -ano -p tcp |
        ForEach-Object {
            if ($_ -match $pattern) { [int]$Matches[1] }
        } |
        Where-Object { $_ -and $_ -ne $PID } |
        Select-Object -Unique)
}

function Stop-PortProcesses([int]$Port) {
    $processIds = Get-PortProcessIds $Port
    if (-not $processIds -or $processIds.Count -eq 0) {
        Write-Host "Port $Port is clear."
        return
    }

    foreach ($processId in $processIds) {
        $commandLine = (Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue).CommandLine
        Write-Host "Stopping PID $processId on port $Port"
        if ($commandLine) { Write-Host "  $commandLine" }
        Stop-Process -Id $processId -Force -ErrorAction Stop
    }
}

Stop-PortProcesses $BackendPort
Stop-PortProcesses $FrontendPort
Write-Host "Trading Agent backend/frontend ports stopped. MongoDB was left running."
