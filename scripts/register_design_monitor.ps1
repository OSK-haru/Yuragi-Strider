$ErrorActionPreference = 'Stop'
$taskName = 'QuantumSim-DesignMonitor'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runner = Join-Path $PSScriptRoot 'run_design_monitor.ps1'
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw "Monitor runner is missing: $runner"
}
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw "Task already exists: $taskName. Review it before changing its schedule."
}

$powershellExe = Join-Path $PSHOME 'powershell.exe'
$action = New-ScheduledTaskAction -Execute $powershellExe `
    -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "' + $runner + '"') `
    -WorkingDirectory $repoRoot
$morning = New-ScheduledTaskTrigger -Daily -At '09:00'
$evening = New-ScheduledTaskTrigger -Daily -At '21:00'
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 50) `
    -RunOnlyIfNetworkAvailable -StartWhenAvailable
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $userId `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action `
    -Trigger @($morning, $evening) -Settings $settings -Principal $principal `
    -Description 'Read-only Codex design monitor for Quantum-sim every 12 hours.' |
    Out-Null
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State, TaskPath
