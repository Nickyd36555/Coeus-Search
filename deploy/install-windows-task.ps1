<#
.SYNOPSIS
  Keep fbmarket running on Windows via Task Scheduler.

.DESCRIPTION
  Registers a scheduled task that starts `fbmarket watch` when you log in and
  restarts it if it stops. Run this from the project folder, in the SAME
  PowerShell window where your virtual environment works.

    .\deploy\install-windows-task.ps1

  To remove it later:

    Unregister-ScheduledTask -TaskName fbmarket -Confirm:$false
#>

[CmdletBinding()]
param(
    [string] $TaskName = "fbmarket"
)

$ErrorActionPreference = "Stop"

# The script lives in deploy\, so the project root is its parent.
$root = Split-Path -Parent $PSScriptRoot
$exe  = Join-Path $root ".venv\Scripts\fbmarket.exe"

if (-not (Test-Path $exe)) {
    Write-Error @"
Could not find $exe

Create the virtual environment and install the project first:

    python -m venv .venv
    .venv\Scripts\Activate.ps1
    pip install -e ".[web]"
"@
    exit 1
}

if (-not (Test-Path (Join-Path $root "config.yaml"))) {
    Write-Warning "No config.yaml in $root - fbmarket will fail to start until you create one."
}

$action = New-ScheduledTaskAction -Execute $exe -Argument "watch" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn

# Keep it alive across transient failures, and never let Windows stop it for
# running "too long" - watching Marketplace is meant to run indefinitely.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -RestartCount 999 `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Replacing the existing '$TaskName' task..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Watch Facebook Marketplace and send alerts on new listings." | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "Registered and started '$TaskName'." -ForegroundColor Green
Write-Host "  Check it:  Get-ScheduledTask -TaskName $TaskName"
Write-Host "  Stop it:   Stop-ScheduledTask -TaskName $TaskName"
Write-Host "  Remove it: Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
Write-Host ""
Write-Host "Note: this starts at logon, so it only runs while you are logged in." -ForegroundColor Yellow
Write-Host "Make sure Windows is set to never sleep, or monitoring pauses when it does."
