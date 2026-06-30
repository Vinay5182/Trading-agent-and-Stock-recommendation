# status-trading-agent.ps1
# Reports the status of the Trading Agent environment. Read-only.
param(
    [int]$BackendPort = 8011,
    [int]$FrontendPort = 5173,
    [int]$MongoPort = 27017,
    [int]$TradingViewPort = 9222
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot

Import-Module -Name (Join-Path $ProjectRoot "scripts\TradingAgent.Processes.psm1") -Force

Write-Host "========================================="
Write-Host "       TRADING AGENT STATUS REPORT       "
Write-Host "========================================="
Write-Host ""

# Get status
$status = Get-TradingAgentStatus -BackendPort $BackendPort -FrontendPort $FrontendPort
Write-Host "OVERALL STATUS: " -NoNewline
if ($status -eq "RUNNING") {
    Write-Host "RUNNING" -ForegroundColor Green
} elseif ($status -eq "PARTIAL") {
    Write-Host "PARTIAL (Incomplete/Unhealthy)" -ForegroundColor Yellow
} elseif ($status -eq "CONFLICT") {
    Write-Host "CONFLICT (Port occupied by unrelated process)" -ForegroundColor Red
} else {
    Write-Host "STOPPED" -ForegroundColor Gray
}
Write-Host ""

# Runtime file state
$runtime = Read-RuntimeOwnership
Write-Host "--- Runtime Ownership File ---"
$runtimeFile = Join-Path $ProjectRoot ".runtime\trading-agent-processes.json"
if (Test-Path -LiteralPath $runtimeFile) {
    Write-Host "File Location: $runtimeFile"
    if ($runtime) {
        Write-Host "Project Root: $($runtime.project_root)"
        Write-Host "Started At:   $($runtime.started_at)"

        # Check backend stale PID
        if ($runtime.backend) {
            $live = Get-ProcessByPid $runtime.backend.pid
            $stale = $true
            if ($live -and ($live.CreationTicks -eq $runtime.backend.creation_ticks)) {
                $stale = $false
            }
            Write-Host "Backend PID:  $($runtime.backend.pid) (Stale: $($stale))"
        }
        # Check frontend stale PID
        if ($runtime.frontend) {
            $live = Get-ProcessByPid $runtime.frontend.pid
            $stale = $true
            if ($live -and ($live.CreationTicks -eq $runtime.frontend.creation_ticks)) {
                $stale = $false
            }
            Write-Host "Frontend PID: $($runtime.frontend.pid) (Stale: $($stale))"
        }
    } else {
        Write-Host "State: Corrupted or empty" -ForegroundColor Red
    }
} else {
    Write-Host "File State: Missing (Not started or stopped cleanly)"
}
Write-Host ""

# Service Ports & Listeners
Write-Host "--- Service Listeners ---"
$backendOwners = @(Get-PortOwners $BackendPort)
if ($backendOwners.Count -gt 0) {
    foreach ($owner in $backendOwners) {
        $isOwned = Test-ProjectOwnedProcess $owner
        Write-Host "Port $BackendPort (Backend):"
        Write-Host "  PID:          $($owner.Pid)"
        Write-Host "  Process Name: $($owner.Name)"
        Write-Host "  Project Owned: $($isOwned)"
        Write-Host "  Command Line: $($owner.CommandLine)"
        if (-not $isOwned) {
            Write-Host "  [CONFLICT] Unrelated process occupying backend port!" -ForegroundColor Red
        }
    }
} else {
    Write-Host "Port $BackendPort (Backend): No listener active"
}

$frontendOwners = @(Get-PortOwners $FrontendPort)
if ($frontendOwners.Count -gt 0) {
    foreach ($owner in $frontendOwners) {
        $isOwned = Test-ProjectOwnedProcess $owner
        Write-Host "Port $FrontendPort (Frontend):"
        Write-Host "  PID:          $($owner.Pid)"
        Write-Host "  Process Name: $($owner.Name)"
        Write-Host "  Project Owned: $($isOwned)"
        Write-Host "  Command Line: $($owner.CommandLine)"
        if (-not $isOwned) {
            Write-Host "  [CONFLICT] Unrelated process occupying frontend port!" -ForegroundColor Red
        }
    }
} else {
    Write-Host "Port $FrontendPort (Frontend): No listener active"
}
Write-Host ""

# Service Health & Integrity
Write-Host "--- Service Health ---"
$backendHealth = Test-BackendHealth -Port $BackendPort
$frontendHealth = Test-FrontendHealth -Port $FrontendPort
$runtimeRoot = Get-BackendRuntimeRoot -BackendPort $BackendPort

Write-Host "Backend Health (/health): " -NoNewline
if ($backendHealth) { Write-Host "OK" -ForegroundColor Green } else { Write-Host "UNHEALTHY / DOWN" -ForegroundColor Red }

Write-Host "Backend Runtime Root:     " -NoNewline
if ($runtimeRoot) {
    $normRoot = ([System.IO.Path]::GetFullPath($runtimeRoot)).TrimEnd("\")
    $normProj = ([System.IO.Path]::GetFullPath($ProjectRoot)).TrimEnd("\")
    if ($normRoot -eq $normProj) {
        Write-Host "$runtimeRoot (MATCH)" -ForegroundColor Green
    } else {
        Write-Host "$runtimeRoot (MISMATCH! Expected: $ProjectRoot)" -ForegroundColor Red
    }
} else {
    Write-Host "N/A (Backend down)"
}

Write-Host "Frontend Reachability:    " -NoNewline
if ($frontendHealth) { Write-Host "OK" -ForegroundColor Green } else { Write-Host "UNHEALTHY / DOWN" -ForegroundColor Red }
Write-Host ""

# External Dependencies
Write-Host "--- Dependencies ---"
$mongoConnected = Test-PortListening $MongoPort
$tvConnected = Test-PortListening $TradingViewPort
$attachedTarget = Get-AttachedTargetInfo -BackendPort $BackendPort

Write-Host "MongoDB (Port $MongoPort):     " -NoNewline
if ($mongoConnected) { Write-Host "REACHABLE" -ForegroundColor Green } else { Write-Host "UNREACHABLE" -ForegroundColor Red }

Write-Host "TradingView CDP ($TradingViewPort):   " -NoNewline
if ($tvConnected) { Write-Host "REACHABLE" -ForegroundColor Green } else { Write-Host "UNREACHABLE" -ForegroundColor Red }

Write-Host "Attached TV Chart Target: " -NoNewline
if ($attachedTarget) {
    Write-Host "$($attachedTarget.title)" -ForegroundColor Green
    Write-Host "  Target ID: $($attachedTarget.target_id)"
    Write-Host "  Target URL: $($attachedTarget.url)"
} else {
    Write-Host "None" -ForegroundColor Yellow
}
Write-Host "========================================="
