# stop-trading-agent.ps1
# Stops the Trading Agent processes safely and cleans up the runtime environment.

param(
    [int]$BackendPort = 8011,
    [int]$FrontendPort = 5173,
    [switch]$ForceKillUnrelatedPortOwner
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot

Import-Module -Name (Join-Path $ProjectRoot "scripts\TradingAgent.Processes.psm1") -Force

Write-Host "Stopping Trading Agent processes..."

# 1. Identify listeners on required ports
$backendOwners = @(Get-PortOwners $BackendPort)
$frontendOwners = @(Get-PortOwners $FrontendPort)
$allPortOwners = @($backendOwners) + @($frontendOwners)

$unrelatedOwners = @()
$projectOwners = @()

foreach ($owner in $allPortOwners) {
    if (Test-ProjectOwnedProcess $owner) {
        $projectOwners += $owner
    } else {
        # Check if already added to avoid duplicates
        $alreadyAdded = $false
        foreach ($unrelated in $unrelatedOwners) {
            if ($unrelated.Pid -eq $owner.Pid) {
                $alreadyAdded = $true
                break
            }
        }
        if (-not $alreadyAdded) {
            $unrelatedOwners += $owner
        }
    }
}

# 2. Check for unrelated port owners
if ($unrelatedOwners.Count -gt 0) {
    Write-Host "Unrelated application(s) occupying required ports detected:" -ForegroundColor Yellow
    foreach ($unrelated in $unrelatedOwners) {
        Write-Host "  PID:             $($unrelated.Pid)"
        Write-Host "  Process Name:    $($unrelated.Name)"
        Write-Host "  Executable Path: $($unrelated.ExecutablePath)"
        Write-Host "  Command Line:    $($unrelated.CommandLine)"
    }

    if (-not $ForceKillUnrelatedPortOwner) {
        Write-Host "Safety Abort: PORT_OCCUPIED_BY_UNRELATED_PROCESS" -ForegroundColor Red
        Write-Host "Use -ForceKillUnrelatedPortOwner parameter if you want to force terminate them."
        exit 1
    } else {
        Write-Host "ForceKillUnrelatedPortOwner specified. Proceeding to terminate unrelated port owners." -ForegroundColor DarkYellow
    }
}

# 3. Collect all project processes to terminate
$pidsToKill = @()

# Add listening project processes
foreach ($owner in $projectOwners) {
    $pidsToKill += $owner
}

# Add unrelated port owners if ForceKill is enabled
if ($ForceKillUnrelatedPortOwner -and $unrelatedOwners.Count -gt 0) {
    foreach ($unrelated in $unrelatedOwners) {
        $pidsToKill += $unrelated
    }
}

# 4. Scan for other project-owned orphan processes (python, node, npm, cmd, esbuild, uvicorn)
$targetNames = @("python", "node", "npm", "cmd", "esbuild", "uvicorn")
$orphans = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $name = $_.Name
    $cmdLine = $_.CommandLine
    if ($cmdLine) {
        $isTarget = $false
        foreach ($t in $targetNames) {
            if ($name -like "*$t*") { $isTarget = $true; break }
        }
        if ($isTarget -and $cmdLine.IndexOf($ProjectRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $true
        } else {
            $false
        }
    } else {
        $false
    }
}

foreach ($orphan in $orphans) {
    # Verify PID reuse
    $ticks = 0
    if ($orphan.CreationDate) { $ticks = $orphan.CreationDate.Ticks }
    $procInfo = [PSCustomObject]@{
        Pid           = $orphan.ProcessId
        Name          = $orphan.Name
        CommandLine   = $orphan.CommandLine
        CreationTicks = $ticks
        ExecutablePath = $orphan.ExecutablePath
    }

    # Avoid duplicates
    $alreadyAdded = $false
    foreach ($item in $pidsToKill) {
        if ($item.Pid -eq $procInfo.Pid) {
            $alreadyAdded = $true
            break
        }
    }
    if (-not $alreadyAdded) {
        $pidsToKill += $procInfo
    }
}

# 5. Terminate the collected processes trees
foreach ($rootProc in $pidsToKill) {
    # Safety Check: Never stop MongoDB, TradingView Desktop, or TradingView CDP
    $name = $rootProc.Name
    if ($name -like "*mongod*" -or $name -like "*TradingView*" -or $name -like "*electron*") {
        Write-Host "Safety: Skipping termination of system process: $name (PID=$($rootProc.Pid))" -ForegroundColor Green
        continue
    }

    $isUnconditional = $false
    if ($ForceKillUnrelatedPortOwner) {
        # Check if this root process was in unrelatedOwners
        foreach ($unrelated in $unrelatedOwners) {
            if ($unrelated.Pid -eq $rootProc.Pid) {
                $isUnconditional = $true
                break
            }
        }
    }

    Stop-ProjectProcessTree -RootProcess $rootProc -Unconditional:$isUnconditional
}

# 6. Verify ports are free (retry up to 3 times)
$portsFree = $false
for ($attempt = 1; $attempt -le 3; $attempt++) {
    Write-Host "Verifying ports are clear (Attempt $attempt/3)..."
    $bFree = Wait-PortFree $BackendPort 3
    $fFree = Wait-PortFree $FrontendPort 3
    if ($bFree -and $fFree) {
        $portsFree = $true
        break
    }
    Start-Sleep -Seconds 1
}

if (-not $portsFree) {
    $remainingB = @(Get-PortOwners $BackendPort)
    $remainingF = @(Get-PortOwners $FrontendPort)
    Write-Host "Failed to free ports. Occupied ports:" -ForegroundColor Red
    foreach ($owner in (@($remainingB) + @($remainingF))) {
        Write-Host "  Port owner: PID=$($owner.Pid), Name=$($owner.Name), Command=$($owner.CommandLine)"
    }
    throw "Port clearance failed: Port $BackendPort or $FrontendPort is still occupied."
}

# 7. Clear the runtime file only after successful shutdown
Clear-RuntimeOwnership
Write-Host "Trading Agent backend/frontend stopped successfully. MongoDB and TradingView were left untouched."
