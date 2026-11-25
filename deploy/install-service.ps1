<#
.SYNOPSIS
    Installs Analytics Scheduler to run automatically in the background under your NT service account.

.DESCRIPTION
    Zero third-party tools required: registers a single resilient Windows Startup task running
    under your NT service account (with automatic restart on failure) — or uses NSSM if present.

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

    [string]$ServiceName = "AnalyticsScheduler"
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

$nssm = Get-Command nssm.exe -ErrorAction SilentlyContinue
if ($nssm) {
    Write-Host "Installing Windows Service '$ServiceName' via NSSM..." -ForegroundColor Cyan
    & nssm.exe install $ServiceName $PythonExe "`"$ServerScript`" --config `"$ConfigPath`""
    & nssm.exe set $ServiceName AppDirectory $InstallDir
    & nssm.exe set $ServiceName Start SERVICE_AUTO_START
    & nssm.exe set $ServiceName AppStdout (Join-Path $InstallDir "data\service-stdout.log")
    & nssm.exe set $ServiceName AppStderr (Join-Path $InstallDir "data\service-stderr.log")
    Write-Host "Set the Log On account ($ServiceAccount) in services.msc or with 'sc config', then start the service." -ForegroundColor Green
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
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $ServiceName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -User $cred.UserName `
    -Password $cred.GetNetworkCredential().Password `
    -RunLevel Highest `
    -Force | Out-Null

Start-ScheduledTask -TaskName $ServiceName
Write-Host "Done! '$ServiceName' is running in the background. Verify at http://127.0.0.1:8787/" -ForegroundColor Green
