$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonExe = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "Project Python is missing: $pythonExe"
}
& $pythonExe (Join-Path $PSScriptRoot 'audit_fleet_dispatch.py')
exit $LASTEXITCODE
