$ErrorActionPreference = 'Stop'
$taskName = 'QuantumSim-AuditFleet'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runner = Join-Path $PSScriptRoot 'run_audit_fleet.ps1'
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw "Audit fleet runner is missing: $runner"
}
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw "Task already exists: $taskName. Review it before changing its schedule."
}

$powershellExe = Join-Path $PSHOME 'powershell.exe'
$action = New-ScheduledTaskAction -Execute $powershellExe `
    -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "' + $runner + '"') `
    -WorkingDirectory $repoRoot
$now = Get-Date
$nextQuarter = $now.Date.AddMinutes(([math]::Floor($now.TimeOfDay.TotalMinutes / 15) + 1) * 15)
$trigger = New-ScheduledTaskTrigger -Once -At $nextQuarter `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 40) `
    -RunOnlyIfNetworkAvailable -StartWhenAvailable
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $userId `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action `
    -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'Bounded audit fleet dispatcher; checks changes every 15 minutes and runs 12-hour roles at 09:00/21:00.' |
    Out-Null
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State, TaskPath
