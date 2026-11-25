<#
.SYNOPSIS
    Installs Analytics Scheduler to run automatically in the background under your NT service account.

.DESCRIPTION
    Zero third-party tools required: registers a single resilient Windows Startup task running
    under your NT service account (with automatic restart on failure) — or uses NSSM if present.
    Runs with least privilege (-RunLevel Limited) by default; pass -HighestPrivileges only if
    your scheduled scripts require elevated administrator rights.

.EXAMPLE
    .\install-service.ps1 `
        -InstallDir "C:\AnalyticsScheduler" `
        -PythonExe "C:\Python311\python.exe" `
        -ServiceAccount "DOMAIN\svc_analytics"
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallDir,

    [Parameter(Mandatory = $true)]
    [string]$PythonExe,

    [Parameter(Mandatory = $true)]
    [string]$ServiceAccount,

    [string]$ServiceName = "AnalyticsScheduler",

    [switch]$HighestPrivileges
)

$ErrorActionPreference = "Stop"

$ServerScript = Join-Path $InstallDir "server.py"
$ConfigPath   = Join-Path $InstallDir "config.json"

if (-not (Test-Path $ServerScript)) {
    throw "Could not find server.py at $ServerScript"
}
if (-not (Test-Path $PythonExe)) {
    throw "Could not find Python executable at $PythonExe"
}

$Port = 8787
$DataDir = Join-Path $InstallDir "data"
if (Test-Path $ConfigPath) {
    try {
        $cfgJson = Get-Content -Path $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($cfgJson.port) {
            $Port = [int]$cfgJson.port
        }
        if ($cfgJson.data_dir) {
            if ([System.IO.Path]::IsPathRooted($cfgJson.data_dir)) {
                $DataDir = $cfgJson.data_dir
            } else {
                $DataDir = Join-Path $InstallDir $cfgJson.data_dir
            }
        }
    } catch {
        Write-Warning "Could not parse $ConfigPath to read port/data_dir; using defaults (port 8787, .\data)."
    }
}

$LogsDir = Join-Path $DataDir "logs"
if (-not (Test-Path $LogsDir)) {
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
}

$nssm = Get-Command nssm.exe -ErrorAction SilentlyContinue
if ($nssm) {
    Write-Host "Installing Windows Service '$ServiceName' via NSSM..." -ForegroundColor Cyan
    & nssm.exe install $ServiceName $PythonExe "`"$ServerScript`" --config `"$ConfigPath`""
    & nssm.exe set $ServiceName AppDirectory $InstallDir
    & nssm.exe set $ServiceName Start SERVICE_AUTO_START
    & nssm.exe set $ServiceName AppStdout (Join-Path $LogsDir "service-stdout.log")
    & nssm.exe set $ServiceName AppStderr (Join-Path $LogsDir "service-stderr.log")
    & nssm.exe set $ServiceName AppRotateFiles 1
    & nssm.exe set $ServiceName AppRotateOnline 1
    & nssm.exe set $ServiceName AppRotateBytes 10485760
    Write-Host "Set the Log On account ($ServiceAccount) in services.msc or with 'sc config', start the service, then verify http://127.0.0.1:$Port/healthz" -ForegroundColor Green
    exit 0
}

Write-Host "Registering single Startup host task '$ServiceName' under $ServiceAccount..." -ForegroundColor Cyan
$cred = Get-Credential -UserName $ServiceAccount -Message "Enter password for NT service account ($ServiceAccount)"

$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$ServerScript`" --config `"$ConfigPath`"" `
    -WorkingDirectory $InstallDir

$trigger = New-ScheduledTaskTrigger -AtStartup

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

$runLevel = if ($HighestPrivileges) { "Highest" } else { "Limited" }

Register-ScheduledTask `
    -TaskName $ServiceName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -User $cred.UserName `
    -Password $cred.GetNetworkCredential().Password `
    -RunLevel $runLevel `
    -Force | Out-Null

Start-ScheduledTask -TaskName $ServiceName

$healthUrl = "http://127.0.0.1:$Port/healthz"
Write-Host "Waiting for '$ServiceName' health check at $healthUrl..." -ForegroundColor Cyan

$healthy = $false
$lastError = $null
for ($i = 1; $i -le 10; $i++) {
    Start-Sleep -Seconds 1
    try {
        $resp = Invoke-RestMethod -Uri $healthUrl -Method Get -TimeoutSec 2
        if ($resp -and $resp.status -eq "ok") {
            $healthy = $true
            break
        }
    } catch {
        $lastError = $_.Exception.Message
    }
}

if (-not $healthy) {
    $serviceLog = Join-Path $LogsDir "service.log"
    throw "Service '$ServiceName' did not respond healthy at $healthUrl within 10 seconds ($lastError). Check $serviceLog and Task Scheduler history, and verify '$ServiceAccount' has 'Log on as a batch job' (SeBatchLogonRight)."
}

Write-Host "Done! '$ServiceName' is running and healthy at $healthUrl" -ForegroundColor Green
