param(
    [int]$BackendPort = 8011,
    [int]$FrontendPort = 5173,
    [int]$MongoPort = 27017
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$BackendRoot = Join-Path $ProjectRoot "backend"
$FrontendRoot = Join-Path $ProjectRoot "frontend"
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$LogRoot = Join-Path $ProjectRoot "logs"
$StartedProcesses = @()

function Normalize-PathValue([string]$PathValue) {
    return ([System.IO.Path]::GetFullPath($PathValue)).TrimEnd("\")
}

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

    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        if ((Get-PortProcessIds $Port).Count -eq 0) { return }
        Start-Sleep -Milliseconds 250
    }
    throw "Port $Port did not clear after stopping existing processes."
}

function Test-PortListening([int]$Port) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connect = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $connect.AsyncWaitHandle.WaitOne(1000, $false)) { return $false }
        $client.EndConnect($connect)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Get-ListeningProcessId([int]$Port) {
    $processId = Get-PortProcessIds $Port | Select-Object -First 1
    if (-not $processId) { throw "No listener found on port $Port." }
    return [int]$processId
}

function Get-ProcessCommandLine([int]$ProcessId) {
    return (Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue).CommandLine
}

function Wait-ProcessCommandLine([int]$ProcessId, [string]$Name) {
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $commandLine = Get-ProcessCommandLine $ProcessId
        if ($commandLine) { return $commandLine }
        Start-Sleep -Milliseconds 250
    }
    throw "$Name process $ProcessId did not expose a command line."
}

function Assert-CommandLineContains([int]$ProcessId, [string]$Name, [string[]]$Needles) {
    $commandLine = Wait-ProcessCommandLine $ProcessId $Name
    foreach ($needle in $Needles) {
        if ($commandLine.IndexOf($needle, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
            throw "$Name process $ProcessId did not start from this project. Missing '$needle' in command line: $commandLine"
        }
    }
    return $commandLine
}

function ConvertTo-ProcessArgumentString([string[]]$Arguments) {
    $escaped = foreach ($argument in $Arguments) {
        if ($argument -notmatch '[\s"]') {
            $argument
        } else {
            '"' + $argument.Replace('"', '\"') + '"'
        }
    }
    return ($escaped -join " ")
}

function Start-HiddenProcess([string]$FilePath, [string[]]$Arguments, [string]$WorkingDirectory) {
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = ConvertTo-ProcessArgumentString $Arguments
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    return [System.Diagnostics.Process]::Start($startInfo)
}

function Format-CommandLine([string]$FilePath, [string[]]$Arguments) {
    return '"' + $FilePath + '" ' + (ConvertTo-ProcessArgumentString $Arguments)
}

function Wait-HttpJson([string]$Uri, [int]$TimeoutSeconds = 45) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastError = $null
    while ((Get-Date) -lt $deadline) {
        try {
            return Invoke-RestMethod -Uri $Uri -Method Get -TimeoutSec 2
        } catch {
            $lastError = $_
            Start-Sleep -Seconds 1
        }
    }
    throw "Timed out waiting for $Uri. Last error: $lastError"
}

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
    $mongoProcess = Start-HiddenProcess $mongodPath $mongoArgs $ProjectRoot
    $script:StartedProcesses += $mongoProcess

    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if (Test-PortListening $MongoPort) { return }
        Start-Sleep -Seconds 1
    }
    throw "MongoDB did not start on port $MongoPort."
}

try {
    if (-not (Test-Path -LiteralPath $BackendRoot)) { throw "Backend folder not found: $BackendRoot" }
    if (-not (Test-Path -LiteralPath $FrontendRoot)) { throw "Frontend folder not found: $FrontendRoot" }
    if (-not (Test-Path -LiteralPath $PythonExe)) { throw "Project virtualenv Python not found: $PythonExe" }
    $npm = (Get-Command "npm.cmd" -ErrorAction Stop).Source
    New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null

    Write-Host "Project root: $ProjectRoot"
    Stop-PortProcesses $BackendPort
    Stop-PortProcesses $FrontendPort
    Start-MongoDbIfNeeded

    $backendArgs = @("-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", [string]$BackendPort, "--app-dir", $BackendRoot)
    $backendCommandLine = Format-CommandLine $PythonExe $backendArgs
    $backendProcess = Start-HiddenProcess $PythonExe $backendArgs $BackendRoot
    $StartedProcesses += $backendProcess

    $frontendArgs = @("/c", $npm, "--prefix", $FrontendRoot, "run", "dev", "--", "--host", "127.0.0.1", "--port", [string]$FrontendPort, "--strictPort")
    $frontendCommandLine = Format-CommandLine $env:ComSpec $frontendArgs
    $frontendProcess = Start-HiddenProcess $env:ComSpec $frontendArgs $FrontendRoot
    $StartedProcesses += $frontendProcess

    $health = Wait-HttpJson "http://127.0.0.1:$BackendPort/health" 60
    if ($health.status -ne "ok") { throw "Backend /health returned unexpected status: $($health | ConvertTo-Json -Compress)" }

    $runtime = Wait-HttpJson "http://127.0.0.1:$BackendPort/api/system/runtime-info" 15
    $backendListeningPid = Get-ListeningProcessId $BackendPort
    $reportedRoot = Normalize-PathValue $runtime.project_root
    if ($reportedRoot -ne (Normalize-PathValue $ProjectRoot)) {
        throw "Backend runtime project_root mismatch. Expected '$ProjectRoot', got '$($runtime.project_root)'."
    }
    if ([int]$runtime.backend_pid -ne $backendListeningPid) {
        throw "Backend runtime PID mismatch. Listener PID $backendListeningPid, runtime reports $($runtime.backend_pid)."
    }

    Wait-HttpJson "http://127.0.0.1:$FrontendPort" 45 | Out-Null
    $frontendListeningPid = Get-ListeningProcessId $FrontendPort

    Write-Host ""
    Write-Host "Trading Agent started."
    Write-Host "Backend PID: $backendListeningPid"
    Write-Host "Backend command line: $backendCommandLine"
    Write-Host "Frontend PID: $frontendListeningPid"
    Write-Host "Frontend command line: $frontendCommandLine"
    Write-Host "Backend:  http://127.0.0.1:$BackendPort"
    Write-Host "Frontend: http://127.0.0.1:$FrontendPort"
} catch {
    Write-Host "Startup failed: $($_.Exception.Message)"
    foreach ($process in $StartedProcesses) {
        try {
            if ($process -and -not $process.HasExited) {
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            }
        } catch {}
    }
    exit 1
}
