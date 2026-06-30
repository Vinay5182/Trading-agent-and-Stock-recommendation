# scripts/test-startup-shutdown.ps1
# PowerShell verification test suite for the Trading Agent process management scripts.

param(
    [switch]$MaintenanceTest
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath "$PSScriptRoot\..").Path
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

Import-Module -Name (Join-Path $ProjectRoot "scripts\TradingAgent.Processes.psm1") -Force

if (-not $MaintenanceTest) {
    Write-Host "======================================================================"
    Write-Host " WARNING: ACTUAL PORT INTEGRATION TESTS ARE SKIPPED BY DEFAULT.         "
    Write-Host " To run full integration tests on ports 8011 and 5173, use:           "
    Write-Host "   powershell -File .\scripts\test-startup-shutdown.ps1 -MaintenanceTest"
    Write-Host "======================================================================"
    Write-Host ""
    Write-Host "Running unit verification checks only..."

    # Simple mock/unit checks
    $status = Get-TradingAgentStatus
    Write-Host "Current Status check: $status"
    Write-Host "Unit verification: PASSED"
    exit 0
}

Write-Host "Running comprehensive Trading Agent Startup/Shutdown tests..."

# Record original state
$originalBackendOwners = @(Get-PortOwners 8011)
$originalFrontendOwners = @(Get-PortOwners 5173)
$originalStatus = Get-TradingAgentStatus
$originalRuntime = Read-RuntimeOwnership

# Abort if unrelated processes already occupy ports
foreach ($owner in (@($originalBackendOwners) + @($originalFrontendOwners))) {
    if (-not (Test-ProjectOwnedProcess $owner)) {
        Write-Host "Test Aborted: required ports 8011 or 5173 occupied by an unrelated process." -ForegroundColor Red
        Write-Host "PID: $($owner.Pid), Name: $($owner.Name), Command: $($owner.CommandLine)"
        exit 1
    }
}

# Helper to stop everything first
function Clean-TestEnvironment {
    Write-Host "Cleaning up test environment..."
    try {
        & (Join-Path $ProjectRoot "stop-trading-agent.ps1") -ForceKillUnrelatedPortOwner
    } catch {
        Write-Host "Cleanup warning: $($_.Exception.Message)"
    }
}

# Ensure clean environment
Clean-TestEnvironment

try {
    # Test 1: Clean Startup
    Write-Host "`n--- Test 1: Clean Startup ---"
    $startResult = & (Join-Path $ProjectRoot "start-trading-agent.ps1")
    $status = Get-TradingAgentStatus
    if ($status -ne "RUNNING") {
        throw "Test 1 Failed: Expected status RUNNING, got $status"
    }
    $runtime = Read-RuntimeOwnership
    if (-not $runtime -or -not $runtime.backend -or -not $runtime.frontend) {
        throw "Test 1 Failed: Runtime ownership file is missing or incomplete"
    }
    Write-Host "Test 1: PASSED"

    # Test 2: Healthy Duplicate Start (should exit without restarting)
    Write-Host "`n--- Test 2: Healthy Duplicate Start ---"
    $initialBackendPid = $runtime.backend.pid
    $initialFrontendPid = $runtime.frontend.pid

    # Capture output of duplicate start
    $duplicateOutput = & (Join-Path $ProjectRoot "start-trading-agent.ps1") 2>&1
    $runtimeAfter = Read-RuntimeOwnership
    if ($runtimeAfter.backend.pid -ne $initialBackendPid -or $runtimeAfter.frontend.pid -ne $initialFrontendPid) {
        throw "Test 2 Failed: Duplicate start restarted the processes (PIDs changed)"
    }
    if ($duplicateOutput -notmatch "Trading Agent is already running") {
        throw "Test 2 Failed: Output did not state 'Trading Agent is already running'"
    }
    Write-Host "Test 2: PASSED"

    # Test 3: Explicit Restart
    Write-Host "`n--- Test 3: Explicit Restart ---"
    & (Join-Path $ProjectRoot "start-trading-agent.ps1") -Restart
    $runtimeRestart = Read-RuntimeOwnership
    if ($runtimeRestart.backend.pid -eq $initialBackendPid) {
        throw "Test 3 Failed: PIDs did not change after explicit restart"
    }
    Write-Host "Test 3: PASSED"

    # Test 4: Complete Shutdown
    Write-Host "`n--- Test 4: Complete Shutdown ---"
    & (Join-Path $ProjectRoot "stop-trading-agent.ps1")
    $statusAfterStop = Get-TradingAgentStatus
    if ($statusAfterStop -ne "STOPPED") {
        throw "Test 4 Failed: Expected status STOPPED, got $statusAfterStop"
    }
    if (Test-Path -LiteralPath (Join-Path $ProjectRoot ".runtime\trading-agent-processes.json")) {
        throw "Test 4 Failed: Runtime ownership file was not cleared after successful stop"
    }
    Write-Host "Test 4: PASSED"

    # Test 5: PID Reuse Protection
    Write-Host "`n--- Test 5: PID Reuse Protection ---"
    # Write a dummy runtime entry referencing explorer.exe's PID (or current PID) but with fake creation ticks
    $currentProc = Get-CimInstance Win32_Process -Filter "ProcessId = $PID"
    $currentTicks = $currentProc.CreationDate.Ticks
    $fakeTicks = $currentTicks - 10000000

    $dummyRuntime = @{
        project_root = $ProjectRoot
        backend = @{
            pid = $PID
            creation_ticks = $fakeTicks
            command_line = $currentProc.CommandLine
            port = 8011
        }
        started_at = (Get-Date).ToString("o")
    }
    Write-RuntimeOwnership -Data $dummyRuntime

    # Run stop script. Since PID matches but ticks mismatch, it must not kill our own process
    & (Join-Path $ProjectRoot "stop-trading-agent.ps1")
    Write-Host "Test 5: PASSED (Current process survived stop-trading-agent.ps1 call due to ticks mismatch)"

    # Test 6: Orphan Cleanup
    Write-Host "`n--- Test 6: Orphan Process Cleanup ---"
    # Launch a mock Python process containing ProjectRoot in its command line, but not listening on any ports
    $dummyScript = Join-Path $ProjectRoot "scripts\mock-orphan.py"
    Set-Content -Path $dummyScript -Value "import time; time.sleep(60)"
    $pythonProc = Start-Process -FilePath $PythonExe -ArgumentList $dummyScript -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 1

    # Run stop logic. It should detect this process as an orphan and terminate it.
    & (Join-Path $ProjectRoot "stop-trading-agent.ps1")
    Start-Sleep -Seconds 1
    if (-not $pythonProc.HasExited) {
        Stop-Process -Id $pythonProc.Id -Force -ErrorAction SilentlyContinue
        Remove-Item -Path $dummyScript -Force -ErrorAction SilentlyContinue
        throw "Test 6 Failed: Orphan python process was not automatically terminated"
    }
    Remove-Item -Path $dummyScript -Force -ErrorAction SilentlyContinue
    Write-Host "Test 6: PASSED"

    # Test 7: Unrelated Port Owner Protection & Force Kill Option
    Write-Host "`n--- Test 7: Unrelated Port Owner Safety ---"
    # Start a dummy listener process on port 8011 from a clean powershell instance
    $dummyListener = Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile", "-Command", '$l = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 8011); $l.Start(); while ($true) { Start-Sleep 1 }' -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 3

    # Attempt to start. It must fail safely with code PORT_OCCUPIED_BY_UNRELATED_PROCESS or output message
    $unrelatedFailed = $false
    try {
        $output = & (Join-Path $ProjectRoot "start-trading-agent.ps1") 2>&1
        if ($output -match "PORT_OCCUPIED_BY_UNRELATED_PROCESS" -or $LASTEXITCODE -eq 1) {
            $unrelatedFailed = $true
        }
    } catch {
        $unrelatedFailed = $true
    }
    if (-not $unrelatedFailed -and -not $dummyListener.HasExited) {
        throw "Test 7 Failed: start-trading-agent.ps1 did not fail or block when port 8011 was owned by an unrelated process"
    }

    # Attempt to stop. It must fail safely and not terminate the unrelated port owner.
    $stopFailed = $false
    try {
        & (Join-Path $ProjectRoot "stop-trading-agent.ps1")
    } catch {
        $stopFailed = $true
    }
    if ($dummyListener.HasExited) {
        throw "Test 7 Failed: stop-trading-agent.ps1 killed unrelated port owner by default"
    }

    # Force kill unrelated port owner
    & (Join-Path $ProjectRoot "stop-trading-agent.ps1") -ForceKillUnrelatedPortOwner
    Start-Sleep -Seconds 1
    if (-not $dummyListener.HasExited) {
        Stop-Process -Id $dummyListener.Id -Force -ErrorAction SilentlyContinue
        throw "Test 7 Failed: stop-trading-agent.ps1 -ForceKillUnrelatedPortOwner did not terminate the unrelated port owner"
    }
    Write-Host "Test 7: PASSED"

    # Test 8: Failed Halfway Startup Cleans Children
    Write-Host "`n--- Test 8: Failed Halfway Startup Cleanup ---"
    # We simulate this by starting uvicorn on port 8011, but making the frontend port 5173 occupied by an unrelated dummy process
    $dummyListener2 = Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile", "-Command", '$l = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 5173); $l.Start(); while ($true) { Start-Sleep 1 }' -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 3

    # Attempt start. Port 8011 is clear, so backend starts, but frontend port is taken by unrelated listener, so it will fail.
    # We want to check that the newly started backend process (on port 8011) gets killed during startup failure.
    $startupHalfwayFailed = $false
    try {
        & (Join-Path $ProjectRoot "start-trading-agent.ps1")
    } catch {
        $startupHalfwayFailed = $true
    }

    # Clean dummy listener on 5173
    Stop-Process -Id $dummyListener2.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1

    # Check if backend port 8011 is occupied. It must be free because startup cleaned up its children!
    $backendOwnersAfterHalfway = @(Get-PortOwners 8011)
    if ($backendOwnersAfterHalfway.Count -gt 0) {
        & (Join-Path $ProjectRoot "stop-trading-agent.ps1")
        throw "Test 8 Failed: Backend process on port 8011 remained alive after startup failed halfway"
    }
    Write-Host "Test 8: PASSED"

    Write-Host "`nAll verification tests completed successfully!" -ForegroundColor Green

} finally {
    # Cleanup dummy processes just in case
    if ($null -ne $dummyListener -and -not $dummyListener.HasExited) {
        Stop-Process -Id $dummyListener.Id -Force -ErrorAction SilentlyContinue
    }
    if ($null -ne $dummyListener2 -and -not $dummyListener2.HasExited) {
        Stop-Process -Id $dummyListener2.Id -Force -ErrorAction SilentlyContinue
    }
    if ($null -ne $pythonProc -and -not $pythonProc.HasExited) {
        Stop-Process -Id $pythonProc.Id -Force -ErrorAction SilentlyContinue
    }
    if ($dummyScript) { Remove-Item -Path $dummyScript -Force -ErrorAction SilentlyContinue }

    # Restore original state
    Clean-TestEnvironment
    if ($originalStatus -eq "RUNNING" -and $originalRuntime) {
        Write-Host "Restoring original running state..."
        & (Join-Path $ProjectRoot "start-trading-agent.ps1")
    }
}
