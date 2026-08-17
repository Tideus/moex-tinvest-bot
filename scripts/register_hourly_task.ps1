param(
    [switch]$Replace
)

$ErrorActionPreference = "Stop"
$taskName = "MOEX-TInvest-Shadow-Hourly"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runnerPath = Join-Path $projectRoot "scripts\run_hourly_shadow.ps1"
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -ne $existing -and -not $Replace) {
    throw "Scheduled task already exists. Inspect it or rerun with -Replace."
}

$now = Get-Date
$firstRun = Get-Date -Hour $now.Hour -Minute 5 -Second 0
if ($firstRun -le $now) {
    $firstRun = $firstRun.AddHours(1)
}
$powershellArgs = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$runnerPath`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $powershellArgs `
    -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Once -At $firstRun `
    -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -StartWhenAvailable

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Read-only MOEX hourly shadow cycle; no broker execution" `
    -Force | Out-Null

Write-Output "Registered $taskName; first run: $firstRun; interval: 1 hour"
