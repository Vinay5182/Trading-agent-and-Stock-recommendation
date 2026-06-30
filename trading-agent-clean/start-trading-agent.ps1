# start-trading-agent.ps1
# Safe startup script for the Trading Agent backend and frontend.

param(
    [int]$BackendPort = 8011,
    [int]$FrontendPort = 5173,
    [int]$MongoPort = 27017,
    [switch]$Restart,
    [switch]$ForceKillUnrelatedPortOwner
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$BackendRoot = Join-Path $ProjectRoot "backend"
$FrontendRoot = Join-Path $ProjectRoot "frontend"
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$LogRoot = Join-Path $ProjectRoot "logs"

Import-Module -Name (Join-Path $ProjectRoot "scripts\TradingAgent.Processes.psm1") -Force

# Helper function to get full path normalization
function Normalize-PathValue([string]$PathValue) {
    return ([System.IO.Path]::GetFullPath($PathValue)).TrimEnd("\")
}

# 1. Duplicate-start check
$status = Get-TradingAgentStatus
if ($status -eq "RUNNING" -and -not $Restart) {
    # Check if this running instance really belongs to this project
    $runtimeRoot = Get-BackendRuntimeRoot -BackendPort $BackendPort
    if ($runtimeRoot -and (Normalize-PathValue $runtimeRoot) -eq (Normalize-PathValue $ProjectRoot)) {
        Write-Host "Trading Agent is already running"
        exit 0
    }
}

# 2. Stop stale or existing same-project processes first
Write-Host "Cleaning up existing project instances..."
$stopParams = @{
    BackendPort = $BackendPort
    FrontendPort = $FrontendPort
}
if ($ForceKillUnrelatedPortOwner) {
    $stopParams.ForceKillUnrelatedPortOwner = $true
}
& (Join-Path $ProjectRoot "stop-trading-agent.ps1") @stopParams

# 3. Verify ports 8011 and 5173 are free
if (-not (Wait-PortFree $BackendPort 5) -or -not (Wait-PortFree $FrontendPort 5)) {
    $bOwners = Get-PortOwners $BackendPort
    $fOwners = Get-PortOwners $FrontendPort
    Write-Host "Startup safety check failed: required ports are occupied by another process!" -ForegroundColor Red
    foreach ($owner in ($bOwners + $fOwners)) {
        Write-Host "  Occupied by: PID=$($owner.Pid), Name=$($owner.Name), Executable=$($owner.ExecutablePath), Command=$($owner.CommandLine)"
    }
    Write-Host "Aborting. Exit Code: PORT_OCCUPIED_BY_UNRELATED_PROCESS" -ForegroundColor Red
    exit 1
}

# MongoDB startup function (preserved)
function Start-MongoDbIfNeeded {
    if (Test-PortListening $MongoPort) {
        Write-Host "MongoDB already listening on port $MongoPort."
        return
    }

    $service = Get-Service -Name "MongoDB" -ErrorAction SilentlyContinue
    if ($service) {
        Write-Host "Starting MongoDB service."
        try {
            Start-Service -Name "MongoDB" -ErrorAction Stop
        } catch {
            Write-Host "MongoDB service did not start from this shell: $($_.Exception.Message)"
        }
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            if (Test-PortListening $MongoPort) { return }
            Start-Sleep -Seconds 1
        }
    }

    if (Test-PortListening $MongoPort) { return }

    $mongod = Get-Command "mongod.exe" -ErrorAction SilentlyContinue
    $mongodPath = if ($mongod) { $mongod.Source } else { $null }
    if (-not $mongodPath) {
        $standardMongoPaths = @(
            "C:\Program Files\MongoDB\Server\8.2\bin\mongod.exe",
            "C:\Program Files\MongoDB\Server\8.1\bin\mongod.exe",
            "C:\Program Files\MongoDB\Server\8.0\bin\mongod.exe",
            "C:\Program Files\MongoDB\Server\7.0\bin\mongod.exe",
            "C:\Program Files\MongoDB\Server\6.0\bin\mongod.exe",
            "C:\Program Files\MongoDB\Server\5.0\bin\mongod.exe",
            "C:\Program Files\MongoDB\Server\4.4\bin\mongod.exe"
        )
        $mongodPath = $standardMongoPaths | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    }
    if (-not $mongodPath) {
        throw "MongoDB is not listening on port $MongoPort, the MongoDB service is unavailable, and mongod.exe was not found in PATH."
    }

    $mongoData = Join-Path $ProjectRoot ".mongo-data"
    New-Item -ItemType Directory -Force -Path $mongoData | Out-Null
    New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
    $mongoLog = Join-Path $LogRoot "mongodb.log"
    $mongoArgs = @("--dbpath", $mongoData, "--bind_ip", "127.0.0.1", "--port", [string]$MongoPort, "--logpath", $mongoLog, "--logappend")
    Write-Host "Starting mongod.exe with project-local dbpath."

    # Hidden process launch
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $mongodPath
    $startInfo.Arguments = ($mongoArgs -join " ")
    $startInfo.WorkingDirectory = $ProjectRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    [System.Diagnostics.Process]::Start($startInfo) | Out-Null

    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if (Test-PortListening $MongoPort) { return }
        Start-Sleep -Seconds 1
    }
    throw "MongoDB did not start on port $MongoPort."
}

# Hidden process startup helper
function Start-HiddenProcess([string]$FilePath, [string[]]$Arguments, [string]$WorkingDirectory) {
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    # Escape quotes if args have spaces
    $escaped = foreach ($arg in $Arguments) {
        if ($arg -notmatch '[\s"]') { $arg } else { '"' + $arg.Replace('"', '\"') + '"' }
    }
    $startInfo.Arguments = ($escaped -join " ")
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    return [System.Diagnostics.Process]::Start($startInfo)
}

# Main startup flow
try {
    if (-not (Test-Path -LiteralPath $BackendRoot)) { throw "Backend folder not found: $BackendRoot" }
    if (-not (Test-Path -LiteralPath $FrontendRoot)) { throw "Frontend folder not found: $FrontendRoot" }
    if (-not (Test-Path -LiteralPath $PythonExe)) { throw "Project virtualenv Python not found: $PythonExe" }
    $npm = (Get-Command "npm.cmd" -ErrorAction Stop).Source
    New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null

    Write-Host "Project root: $ProjectRoot"
    Start-MongoDbIfNeeded

    # Generate diagnostics token and configure env
    $diagToken = [System.Guid]::NewGuid().ToString("N")
    $env:TRADING_AGENT_DIAGNOSTICS_ENABLED = "true"
    $env:TRADING_AGENT_DIAGNOSTICS_TOKEN = $diagToken

    # 4. Start backend
    $backendArgs = @("-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", [string]$BackendPort, "--app-dir", $BackendRoot)
    Write-Host "Launching Backend process..."
    $backendProcess = Start-HiddenProcess $PythonExe $backendArgs $BackendRoot
    $backendPid = $backendProcess.Id

    # 5. Start frontend
    $frontendArgs = @("/c", $npm, "--prefix", $FrontendRoot, "run", "dev", "--", "--host", "127.0.0.1", "--port", [string]$FrontendPort, "--strictPort")
    Write-Host "Launching Frontend process..."
    $frontendProcess = Start-HiddenProcess $env:ComSpec $frontendArgs $FrontendRoot
    $frontendPid = $frontendProcess.Id

    # 6. Wait and Validate Backend Health
    Write-Host "Waiting for Backend health check..."
    $backendHealthy = $false
    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline) {
        if (Test-BackendHealth -Port $BackendPort) {
            $backendHealthy = $true
            break
        }
        if ($backendProcess.HasExited) {
            throw "Backend process exited prematurely during startup."
        }
        Start-Sleep -Seconds 1
    }

    if (-not $backendHealthy) {
        throw "Timed out waiting for Backend /health to return OK."
    }

    # 7. Verify /api/system/runtime-info project_root
    $runtimeRoot = Get-BackendRuntimeRoot -BackendPort $BackendPort
    if (-not $runtimeRoot) {
        throw "Failed to retrieve system runtime root from backend."
    }
    if ((Normalize-PathValue $runtimeRoot) -ne (Normalize-PathValue $ProjectRoot)) {
        # Check case-insensitive match
        if ($runtimeRoot.ToLower().TrimEnd("\") -ne $ProjectRoot.ToLower().TrimEnd("\")) {
            throw "Backend runtime project_root mismatch. Expected '$ProjectRoot', got '$runtimeRoot'."
        }
    }

    # 8. Wait and Validate Frontend Health
    Write-Host "Waiting for Frontend to respond..."
    $frontendHealthy = $false
    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline) {
        if (Test-FrontendHealth -Port $FrontendPort) {
            $frontendHealthy = $true
            break
        }
        if ($frontendProcess.HasExited) {
            throw "Frontend process exited prematurely during startup."
        }
        Start-Sleep -Seconds 1
    }

    if (-not $frontendHealthy) {
        throw "Timed out waiting for Frontend HTTP 200 check."
    }

    # 9. Verify launched listener PIDs actually belong to this project
    $bOwners = @(Get-PortOwners $BackendPort)
    $fOwners = @(Get-PortOwners $FrontendPort)
    $allNew = @($bOwners) + @($fOwners)
    foreach ($newOwner in $allNew) {
        if (-not (Test-ProjectOwnedProcess $newOwner)) {
            throw "Post-startup validation failed: port listener PID $($newOwner.Pid) does not belong to this project."
        }
    }

    # 10. Record process identity in the runtime ownership registry
    $backendInfo = Get-ProcessByPid $backendPid
    $frontendInfo = Get-ProcessByPid $frontendPid

    # Resolve child processes (descendants of backend and frontend)
    $bDescendants = Get-ProcessTree $backendPid
    $fDescendants = Get-ProcessTree $frontendPid
    $childProcesses = @()
    foreach ($child in (@($bDescendants) + @($fDescendants))) {
        $childProcesses += @{
            pid            = $child.Pid
            name           = $child.Name
            creation_ticks = $child.CreationTicks
            command_line   = $child.CommandLine
        }
    }

    $runtimeData = @{
        project_root = $ProjectRoot
        diagnostics_token = $diagToken
        backend      = @{
            pid            = $backendPid
            creation_ticks = $backendInfo.CreationTicks
            command_line   = $backendInfo.CommandLine
            port           = $BackendPort
        }
        frontend     = @{
            pid            = $frontendPid
            creation_ticks = $frontendInfo.CreationTicks
            command_line   = $frontendInfo.CommandLine
            port           = $FrontendPort
        }
        child_processes = $childProcesses
        started_at      = (Get-Date).ToString("o")
    }
    Write-RuntimeOwnership -Data $runtimeData

    Write-Host ""
    Write-Host "Trading Agent started successfully." -ForegroundColor Green
    Write-Host "Backend PID:  $backendPid"
    Write-Host "Frontend PID: $frontendPid"
    Write-Host "Backend:  http://127.0.0.1:$BackendPort"
    Write-Host "Frontend: http://127.0.0.1:$FrontendPort"

} catch {
    Write-Host "Startup failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Stopping any newly launched processes..." -ForegroundColor Yellow

    # Stop newly launched PIDs if validation fails halfway
    if ($null -ne $backendProcess -and -not $backendProcess.HasExited) {
        try {
            $ticks = (Get-CimInstance Win32_Process -Filter "ProcessId = $($backendProcess.Id)" -ErrorAction SilentlyContinue).CreationDate.Ticks
            $bProc = [PSCustomObject]@{ Pid = $backendProcess.Id; Name = "python"; CreationTicks = $ticks; CommandLine = "" }
            Stop-ProjectProcessTree -RootProcess $bProc -Unconditional
        } catch {}
    }
    if ($null -ne $frontendProcess -and -not $frontendProcess.HasExited) {
        try {
            $ticks = (Get-CimInstance Win32_Process -Filter "ProcessId = $($frontendProcess.Id)" -ErrorAction SilentlyContinue).CreationDate.Ticks
            $fProc = [PSCustomObject]@{ Pid = $frontendProcess.Id; Name = "cmd"; CreationTicks = $ticks; CommandLine = "" }
            Stop-ProjectProcessTree -RootProcess $fProc -Unconditional
        } catch {}
    }
    exit 1
}
