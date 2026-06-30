# Trading Agent Process Management Module

$ErrorActionPreference = "Stop"

$ModuleRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path -LiteralPath "$ModuleRoot\..").Path
$RuntimeDir = Join-Path $ProjectRoot ".runtime"
$RuntimeFile = Join-Path $RuntimeDir "trading-agent-processes.json"

# Helper: Read Runtime file
function Read-RuntimeOwnership {
    if (-not (Test-Path -LiteralPath $RuntimeFile)) {
        return $null
    }
    try {
        $content = Get-Content -LiteralPath $RuntimeFile -Raw -ErrorAction SilentlyContinue
        if ([string]::IsNullOrWhiteSpace($content)) { return $null }
        return ConvertFrom-Json $content
    } catch {
        return $null
    }
}

# Helper: Write Runtime file
function Write-RuntimeOwnership {
    param(
        [Parameter(Mandatory=$true)]
        [object]$Data
    )
    if (-not (Test-Path -LiteralPath $RuntimeDir)) {
        New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
    }
    $json = ConvertTo-Json $Data -Depth 5
    Set-Content -LiteralPath $RuntimeFile -Value $json -Force
}

# Helper: Clear Runtime file
function Clear-RuntimeOwnership {
    if (Test-Path -LiteralPath $RuntimeFile) {
        Remove-Item -LiteralPath $RuntimeFile -Force -ErrorAction SilentlyContinue
    }
}

# Helper: Get Live Process Info by PID
function Get-ProcessByPid([int]$Pid) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $Pid" -ErrorAction SilentlyContinue
    if ($proc) {
        $ticks = 0
        if ($proc.CreationDate) {
            $ticks = $proc.CreationDate.Ticks
        }
        return [PSCustomObject]@{
            Pid           = $proc.ProcessId
            Name          = $proc.Name
            CommandLine   = $proc.CommandLine
            CreationTicks = $ticks
            ExecutablePath = $proc.ExecutablePath
        }
    }
    return $null
}

# Find every process ID listening on a given port
function Get-PortOwners([int]$Port) {
    $pattern = "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$"
    $pids = @(& netstat -ano -p tcp |
        ForEach-Object {
            if ($_ -match $pattern) { [int]$Matches[1] }
        } |
        Where-Object { $_ -and $_ -ne $PID } |
        Select-Object -Unique)

    $owners = @()
    foreach ($pid in $pids) {
        $procInfo = Get-ProcessByPid $pid
        if ($procInfo) {
            $owners += $procInfo
        }
    }
    return $owners
}

# Get entire process tree of descendants for a parent PID
function Get-ProcessTree([int]$ParentPid) {
    $tree = @()
    $queue = @($ParentPid)
    while ($queue.Count -gt 0) {
        $current = $queue[0]
        if ($queue.Count -gt 1) {
            $queue = $queue[1..($queue.Count-1)]
        } else {
            $queue = @()
        }

        $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $current" -ErrorAction SilentlyContinue
        foreach ($child in $children) {
            if ($child.ProcessId -eq $PID) { continue } # Do not terminate ourselves
            $alreadyInTree = $false
            foreach ($item in $tree) {
                if ($item.Pid -eq $child.ProcessId) {
                    $alreadyInTree = $true
                    break
                }
            }
            if (-not $alreadyInTree) {
                $ticks = 0
                if ($child.CreationDate) { $ticks = $child.CreationDate.Ticks }
                $procInfo = [PSCustomObject]@{
                    Pid           = $child.ProcessId
                    Name          = $child.Name
                    CommandLine   = $child.CommandLine
                    CreationTicks = $ticks
                    ExecutablePath = $child.ExecutablePath
                }
                $tree += $procInfo
                $queue += $child.ProcessId
            }
        }
    }
    # Return descendants first (deepest first) by reversing list
    [array]::Reverse($tree)
    return $tree
}

# Test if a process belongs to this project
function Test-ProjectOwnedProcess {
    param(
        [Parameter(Mandatory=$true)]
        [object]$ProcessInfo
    )
    if (-not $ProcessInfo) { return $false }
    $cmd = $ProcessInfo.CommandLine
    if (-not $cmd) { return $false }

    # Option A: Check if the command line contains the project root path
    if ($cmd.IndexOf($ProjectRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
        return $true
    }

    # Option B: Check against the persistent .runtime file registry
    $runtime = Read-RuntimeOwnership
    if ($runtime) {
        # Check backend
        if ($runtime.backend -and $runtime.backend.pid -eq $ProcessInfo.Pid -and $runtime.backend.creation_ticks -eq $ProcessInfo.CreationTicks) {
            return $true
        }
        # Check frontend
        if ($runtime.frontend -and $runtime.frontend.pid -eq $ProcessInfo.Pid -and $runtime.frontend.creation_ticks -eq $ProcessInfo.CreationTicks) {
            return $true
        }
        # Check children
        if ($runtime.child_processes) {
            foreach ($child in $runtime.child_processes) {
                if ($child.pid -eq $ProcessInfo.Pid -and $child.creation_ticks -eq $ProcessInfo.CreationTicks) {
                    return $true
                }
            }
        }
    }

    return $false
}

# Safe termination of a single process tree
function Stop-ProjectProcessTree {
    param(
        [Parameter(Mandatory=$true)]
        [object]$RootProcess,
        [switch]$Unconditional
    )

    if (-not $RootProcess) { return }

    # Resolve descendants first
    $descendants = Get-ProcessTree $RootProcess.Pid
    $allToStop = @($descendants) + @($RootProcess)

    foreach ($proc in $allToStop) {
        # Verify process still exists and PID hasn't been reused (matches CreationTicks)
        $live = Get-ProcessByPid $proc.Pid
        if ($live -and ($live.CreationTicks -eq $proc.CreationTicks)) {
            $isOwned = Test-ProjectOwnedProcess $live
            if ($isOwned -or $Unconditional) {
                Write-Host "Stopping Process: PID=$($proc.Pid), Name=$($proc.Name)"
                if ($proc.CommandLine) {
                    Write-Host "  Command: $($proc.CommandLine)"
                }
                try {
                    Stop-Process -Id $proc.Pid -Force -ErrorAction Stop
                } catch {
                    Write-Host "  Warning: Failed to stop process $($proc.Pid): $($_.Exception.Message)"
                }
            } else {
                Write-Host "  Skipping Process PID=$($proc.Pid) (not verified as belonging to this project)"
            }
        }
    }
}

# Wait for a port to be free (returns $true if free, $false if timed out)
function Wait-PortFree {
    param(
        [int]$Port,
        [int]$TimeoutSeconds = 10
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $owners = Get-PortOwners $Port
        if ($owners.Count -eq 0) {
            return $true
        }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

# Test Backend Health
function Test-BackendHealth {
    param([int]$Port = 8011)
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -Method Get -TimeoutSec 2 -ErrorAction Stop
        if ($response -and $response.status -eq "ok") {
            return $true
        }
        return $false
    } catch {
        return $false
    }
}

# Test Frontend Health
function Test-FrontendHealth {
    param([int]$Port = 5173)
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port" -Method Get -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
        $status = $response.StatusCode
        if ($status -eq 200 -or $status -eq 304) {
            return $true
        }
        return $false
    } catch {
        if ($_.Exception -and $_.Exception.Response) {
            $status = [int]$_.Exception.Response.StatusCode
            if ($status -eq 200 -or $status -eq 304) {
                return $true
            }
        }
        return $false
    }
}

# Test generic reachability of a port
function Test-PortListening {
    param([int]$Port)
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

# Get attached TradingView target details
function Get-AttachedTargetInfo {
    param([int]$BackendPort = 8011)
    try {
        $status = Invoke-RestMethod -Uri "http://127.0.0.1:$BackendPort/api/tv/runtime-status" -Method Get -TimeoutSec 2 -ErrorAction Stop
        if ($status -and $status.attached_target) {
            return $status.attached_target
        }
        return $null
    } catch {
        return $null
    }
}

# Check project root in backend runtime-info
function Get-BackendRuntimeRoot {
    param([int]$BackendPort = 8011)
    try {
        $headers = @{}
        $token = $env:TRADING_AGENT_DIAGNOSTICS_TOKEN
        if (-not $token) {
            $runtimeInfo = Read-RuntimeOwnership
            if ($runtimeInfo -and $runtimeInfo.diagnostics_token) {
                $token = $runtimeInfo.diagnostics_token
            }
        }
        if ($token) {
            $headers["X-Trading-Agent-Diagnostics"] = $token
        }
        $runtime = Invoke-RestMethod -Uri "http://127.0.0.1:$BackendPort/api/system/runtime-info" -Method Get -Headers $headers -TimeoutSec 2 -ErrorAction Stop
        if ($runtime -and $runtime.project_root) {
            return $runtime.project_root
        }
        return $null
    } catch {
        return $null
    }
}

# Get overall status
function Get-TradingAgentStatus {
    param(
        [int]$BackendPort = 8011,
        [int]$FrontendPort = 5173
    )
    $backendOwners = Get-PortOwners $BackendPort
    $frontendOwners = Get-PortOwners $FrontendPort

    $backendHealthy = Test-BackendHealth -Port $BackendPort
    $frontendHealthy = Test-FrontendHealth -Port $FrontendPort

    # Check for conflict: any owner exists that is NOT owned by this project
    $hasConflict = $false
    foreach ($owner in (@($backendOwners) + @($frontendOwners))) {
        if (-not (Test-ProjectOwnedProcess $owner)) {
            $hasConflict = $true
            break
        }
    }

    if ($hasConflict) {
        return "CONFLICT"
    }

    if ($backendHealthy -and $frontendHealthy) {
        # Check runtime-root match
        $runtimeRoot = Get-BackendRuntimeRoot -BackendPort $BackendPort
        if ($runtimeRoot) {
            $normRoot = ([System.IO.Path]::GetFullPath($runtimeRoot)).TrimEnd("\")
            $normProj = ([System.IO.Path]::GetFullPath($ProjectRoot)).TrimEnd("\")
            if ($normRoot -eq $normProj) {
                return "RUNNING"
            }
        }
    }

    # If some owned processes are running or listening, it's PARTIAL
    $hasOwned = $false
    foreach ($owner in (@($backendOwners) + @($frontendOwners))) {
        if (Test-ProjectOwnedProcess $owner) { $hasOwned = $true; break }
    }

    # Also check if any processes in runtime file are still active
    $runtime = Read-RuntimeOwnership
    if ($runtime) {
        if ($runtime.backend) {
            $live = Get-ProcessByPid $runtime.backend.pid
            if ($live -and ($live.CreationTicks -eq $runtime.backend.creation_ticks) -and (Test-ProjectOwnedProcess $live)) {
                $hasOwned = $true
            }
        }
        if ($runtime.frontend) {
            $live = Get-ProcessByPid $runtime.frontend.pid
            if ($live -and ($live.CreationTicks -eq $runtime.frontend.creation_ticks) -and (Test-ProjectOwnedProcess $live)) {
                $hasOwned = $true
            }
        }
    }

    if ($hasOwned -or $backendHealthy -or $frontendHealthy) {
        return "PARTIAL"
    }

    if ($backendOwners.Count -gt 0 -or $frontendOwners.Count -gt 0) {
        return "PARTIAL"
    }

    return "STOPPED"
}

Export-ModuleMember -Function `
    Read-RuntimeOwnership, `
    Write-RuntimeOwnership, `
    Clear-RuntimeOwnership, `
    Get-ProcessByPid, `
    Get-PortOwners, `
    Get-ProcessTree, `
    Test-ProjectOwnedProcess, `
    Stop-ProjectProcessTree, `
    Wait-PortFree, `
    Test-BackendHealth, `
    Test-FrontendHealth, `
    Test-PortListening, `
    Get-AttachedTargetInfo, `
    Get-BackendRuntimeRoot, `
    Get-TradingAgentStatus
