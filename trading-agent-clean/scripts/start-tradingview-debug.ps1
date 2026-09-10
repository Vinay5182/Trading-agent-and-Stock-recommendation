# scripts\start-tradingview-debug.ps1
# Portable startup script for TradingView (Desktop App or Chrome) with Remote Debugging Port enabled.

[CmdletBinding()]
param(
    [int]$Port = 9222,
    [string]$ProfileDir = "C:\TradingAgent\chrome-profile",
    [string]$TradingViewUrl = "https://www.tradingview.com/",
    [switch]$ForceRestart
)

$ErrorActionPreference = "Stop"

# Portable relative path resolution
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path -LiteralPath "$ScriptDir\..").Path

# Respect environment variable override if set
if ($env:TRADINGVIEW_DEBUG_PORT) {
    [int]$envPort = 0
    if ([int]::TryParse($env:TRADINGVIEW_DEBUG_PORT, [ref]$envPort) -and $envPort -gt 0) {
        $Port = $envPort
    }
}

Write-Host "========================================" -ForegroundColor DarkCyan
Write-Host " Trading Agent TradingView Setup        " -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor DarkCyan
Write-Host ""

# 1. Locate TradingView Desktop App or Google Chrome Executable
$appType = "Chrome"
$appPath = $null

# Check TradingView Desktop App paths first
$possibleTvPaths = @(
    "C:\Users\Asus\Downloads\TradingView (1)\TradingView.exe",
    "$env:LOCALAPPDATA\Programs\TradingView\TradingView.exe",
    "C:\Program Files\TradingView\TradingView.exe",
    "C:\Program Files (x86)\TradingView\TradingView.exe"
) + @(Get-ChildItem "$env:USERPROFILE\Downloads" -Filter "TradingView*.exe" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)

$foundTvPath = $possibleTvPaths | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1

if ($foundTvPath) {
    $appType = "TradingView Desktop"
    $appPath = $foundTvPath
} else {
    # Fallback to Google Chrome paths
    $possibleChromePaths = @(
        "C:\Program Files\Google\Chrome\Application\chrome.exe",
        "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )

    $foundChromePath = $possibleChromePaths | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

    if (-not $foundChromePath) {
        try {
            $regPath = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" -ErrorAction SilentlyContinue).'(default)'
            if ($regPath -and (Test-Path -LiteralPath $regPath)) {
                $foundChromePath = $regPath
            }
        } catch {}
    }

    if (-not $foundChromePath) {
        $cmdChrome = Get-Command "chrome.exe" -ErrorAction SilentlyContinue
        if ($cmdChrome) {
            $foundChromePath = $cmdChrome.Source
        }
    }

    if ($foundChromePath) {
        $appType = "Google Chrome"
        $appPath = $foundChromePath
    }
}

if (-not $appPath) {
    Write-Host "Browser / App:   FAILED (Not Found)" -ForegroundColor Red
    Write-Host "Error: Neither TradingView Desktop App nor Google Chrome could be located." -ForegroundColor Red
    Write-Host "Please install TradingView Desktop or Google Chrome and try again." -ForegroundColor Yellow
    exit 1
}

$appVersion = (Get-Item $appPath).VersionInfo.ProductVersion

# 2. Ensure Profile Directory Exists (used for Chrome fallback)
if ($appType -eq "Google Chrome" -and -not (Test-Path -LiteralPath $ProfileDir)) {
    Write-Host "Creating dedicated Chrome profile directory at '$ProfileDir'..." -ForegroundColor Yellow
    New-Item -ItemType Directory -Path $ProfileDir -Force | Out-Null
}

# Helper functions for network & CDP tests
function Test-PortListeningLocal([int]$TestPort) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $asyncResult = $client.BeginConnect("127.0.0.1", $TestPort, $null, $null)
        $success = $asyncResult.AsyncWaitHandle.WaitOne(1000, $false)
        if ($success -and $client.Connected) {
            $client.EndConnect($asyncResult)
            return $true
        }
        return $false
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Get-CdpJsonEndpoint([int]$TestPort, [string]$Path) {
    try {
        return Invoke-RestMethod -Uri "http://127.0.0.1:$TestPort/json/$Path" -TimeoutSec 3 -ErrorAction Stop
    } catch {
        return $null
    }
}

# 3. Check Port & Launch App if needed
$portActive = Test-PortListeningLocal $Port

if ($portActive -and $ForceRestart) {
    Write-Host "ForceRestart requested. Closing existing process on port $Port..." -ForegroundColor Yellow
    $procs = Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('chrome.exe', 'TradingView.exe') }
    foreach ($proc in $procs) {
        if ($proc.CommandLine -and $proc.CommandLine.Contains("remote-debugging-port")) {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Seconds 2
    $portActive = Test-PortListeningLocal $Port
}

if (-not $portActive) {
    Write-Host "Launching $appType with remote debugging on port $Port..." -ForegroundColor Cyan
    
    if ($appType -eq "TradingView Desktop") {
        $appArgs = @(
            "--remote-debugging-port=$Port"
        )
    } else {
        $appArgs = @(
            "--remote-debugging-port=$Port",
            "--user-data-dir=`"$ProfileDir`"",
            "`"$TradingViewUrl`""
        )
    }
    
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $appPath
    $startInfo.Arguments = ($appArgs -join " ")
    $startInfo.UseShellExecute = $true
    [System.Diagnostics.Process]::Start($startInfo) | Out-Null

    # Wait for port to listen (up to 15 seconds)
    $portReady = $false
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 1
        if (Test-PortListeningLocal $Port) {
            $portReady = $true
            break
        }
    }

    if (-not $portReady) {
        Write-Host "App Status:      READY" -ForegroundColor Green
        Write-Host "Debug Port:      $Port (FAILED TO LISTEN)" -ForegroundColor Red
        Write-Host "CDP Endpoint:    NOT READY" -ForegroundColor Red
        Write-Host "TradingView:     NOT READY" -ForegroundColor Red
        Write-Host "Error: App launched but debug port $Port is not accepting TCP connections." -ForegroundColor Red
        exit 1
    }
}

# 4. Verify CDP Endpoint and TradingView Target
$cdpVersion = Get-CdpJsonEndpoint -TestPort $Port -Path "version"
$cdpTabs = Get-CdpJsonEndpoint -TestPort $Port -Path "list"

$cdpReady = ($null -ne $cdpVersion)
$tvReady = $false

if ($cdpTabs) {
    foreach ($tab in $cdpTabs) {
        $url = if ($tab.url) { $tab.url } else { "" }
        $title = if ($tab.title) { $tab.title } else { "" }
        if ($url -like "*tradingview.com*" -or $title -like "*TradingView*" -or $appType -eq "TradingView Desktop") {
            $tvReady = $true
            break
        }
    }
}

# 5. Output Standardized Readiness Report
Write-Host "App / Browser:     $appType" -ForegroundColor Gray
Write-Host "Executable Path:   $appPath" -ForegroundColor Gray
Write-Host "App Version:       $appVersion" -ForegroundColor Gray
if ($appType -eq "Google Chrome") {
    Write-Host "Profile Dir:       $ProfileDir" -ForegroundColor Gray
}
Write-Host ""
Write-Host "Application:     READY" -ForegroundColor Green
Write-Host "Debug Port:      $Port" -ForegroundColor Green

if ($cdpReady) {
    Write-Host "CDP Endpoint:    READY" -ForegroundColor Green
} else {
    Write-Host "CDP Endpoint:    NOT READY" -ForegroundColor Red
}

if ($tvReady) {
    Write-Host "TradingView:     READY" -ForegroundColor Green
} else {
    Write-Host "TradingView:     NOT READY (Please open a chart tab in TradingView)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "TradingView debug setup check complete." -ForegroundColor Cyan
Write-Host "You can now start the Trading Agent using: .\start-trading-agent.ps1"
Write-Host "========================================"
