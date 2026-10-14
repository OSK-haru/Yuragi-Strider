param(
    [switch]$CheckOnly,
    [ValidateSet('legacy', '1', '2', '3', '4', '5', '6', '7')]
    [string]$Role = 'legacy',
    [ValidateRange(1, 45)]
    [int]$TimeoutMinutes = 45,
    [string]$ChangedPathsJson = '',
    [switch]$BudgetReserved
)

$ErrorActionPreference = 'Stop'
$auditModel = 'gpt-6-luna'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$instructionsName = if ($Role -eq 'legacy') { 'MONITOR_REVIEW.md' } else { 'AUDIT_FLEET.md' }
$instructions = Join-Path $repoRoot $instructionsName
if (-not (Test-Path -LiteralPath $instructions -PathType Leaf)) {
    throw "Monitor instructions are missing: $instructions"
}

$codexCommand = Get-Command codex.exe -ErrorAction SilentlyContinue
if ($codexCommand) {
    $codexExe = $codexCommand.Source
} else {
    $extensionRoot = Join-Path $env:USERPROFILE '.vscode\extensions'
    $candidates = @(
        Get-ChildItem -Path $extensionRoot -Filter 'openai.chatgpt-*' -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName 'bin\windows-x86_64\codex.exe' } |
            Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
            Sort-Object -Descending
    )
    if ($candidates.Count -eq 0) {
        throw 'Codex CLI was not found; install or expose codex.exe before the scheduled run.'
    }
    $codexExe = $candidates[0]
}
$signature = Get-AuthenticodeSignature -LiteralPath $codexExe
if ($signature.Status -ne 'Valid' -or
    $signature.SignerCertificate.Subject -notlike '*O="OpenAI OpCo, LLC"*') {
    throw "Codex CLI does not have a valid OpenAI signature: $codexExe"
}

if ($CheckOnly) {
    Write-Output "Repository: $repoRoot"
    Write-Output "Instructions: $instructions"
    Write-Output "Codex CLI: $codexExe"
    Write-Output "Model: $auditModel"
    $searchMode = if ($Role -in @('legacy', '1', '2', '4', '7')) { 'enabled' } else { 'disabled' }
    Write-Output "Mode: role $Role; isolated snapshot; web search $searchMode; $TimeoutMinutes-minute timeout"
    exit 0
}
if (-not $BudgetReserved) {
    throw 'Direct model runs are disabled. Use audit_fleet_dispatch.py, which reserves the daily model budget first.'
}

$outputDir = Join-Path $repoRoot '.cache\design-monitor'
if ($Role -ne 'legacy') {
    $outputDir = Join-Path $outputDir "role$Role"
}
New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
$runId = Get-Date -Format 'yyyyMMdd-HHmmss'
$snapshotPath = Join-Path $outputDir "$runId-source"
$manifestPath = Join-Path $outputDir "$runId-manifest.json"
$packetPath = Join-Path $outputDir "$runId-packet.txt"
$reportPath = Join-Path $outputDir "$runId-report.md"
$stdoutPath = Join-Path $outputDir "$runId-stdout.log"
$stderrPath = Join-Path $outputDir "$runId-stderr.log"
$latestPath = Join-Path $outputDir 'latest.md'
$startedAt = Get-Date
$pythonExe = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "Project Python is missing: $pythonExe"
}
$snapshotBuilder = Join-Path $PSScriptRoot 'build_monitor_snapshot.py'
try {
    & $pythonExe $snapshotBuilder create $snapshotPath $manifestPath | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not build the filtered monitor snapshot.'
    }
    if ($Role -eq 'legacy') {
        $packetBuilder = Join-Path $PSScriptRoot 'build_monitor_packet.py'
        & $pythonExe $packetBuilder $snapshotPath $manifestPath $packetPath | Out-Null
    } else {
        $packetBuilder = Join-Path $PSScriptRoot 'build_audit_packet.py'
        $packetArgs = @($Role, $snapshotPath, $manifestPath, $packetPath,
            '--history-root', (Join-Path $repoRoot '.cache\design-monitor'))
        if ($ChangedPathsJson -ne '') {
            $historyRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot '.cache\design-monitor')).TrimEnd('\') + '\'
            $selectedPath = [IO.Path]::GetFullPath($ChangedPathsJson)
            if (-not $selectedPath.StartsWith($historyRoot, [StringComparison]::OrdinalIgnoreCase) -or
                -not (Test-Path -LiteralPath $selectedPath -PathType Leaf)) {
                throw 'Selected path list must be an existing monitor-cache file.'
            }
            $packetArgs += @('--selected', $selectedPath)
        }
        & $pythonExe $packetBuilder @packetArgs | Out-Null
    }
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not build the bounded review packet.'
    }

    # The child receives its normal local sign-in, but no unrelated API secrets.
    foreach ($name in [Environment]::GetEnvironmentVariables().Keys) {
        if ($name -match '(?i)(API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)') {
            Remove-Item -LiteralPath ("Env:" + $name) -ErrorAction SilentlyContinue
        }
    }

    $arguments = @(
        '--ask-for-approval', 'never', 'exec', '--model', $auditModel, '--ephemeral',
        '--ignore-user-config',
        '--sandbox', 'read-only', '--skip-git-repo-check',
        '--cd', ('"' + $snapshotPath + '"'),
        '--output-last-message', ('"' + $reportPath + '"'), '-'
    )
    if ($Role -in @('legacy', '1', '2', '4', '7')) {
        $arguments = @('--search') + $arguments
    }

    $process = Start-Process -FilePath $codexExe -ArgumentList $arguments `
        -WorkingDirectory $snapshotPath -WindowStyle Hidden -PassThru `
        -RedirectStandardInput $packetPath -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath
    $completed = $process.WaitForExit($TimeoutMinutes * 60 * 1000)
    if (-not $completed) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        $status = 'timed_out'
    } else {
        $process.WaitForExit()
        $process.Refresh()
        $exitCode = $process.ExitCode
        if ($null -ne $exitCode -and $exitCode -ne 0) {
            $status = "failed (exit $exitCode)"
        } elseif (-not (Test-Path -LiteralPath $reportPath -PathType Leaf)) {
            $status = 'failed (no report produced)'
        } else {
            $reportText = [IO.File]::ReadAllText($reportPath)
            $lineBreak = $reportText.IndexOf("`n")
            if ($lineBreak -ge 0) {
                $reportText = $reportText.Substring(0, $lineBreak + 1) +
                    "AUDIT_MODEL: $auditModel`n" + $reportText.Substring($lineBreak + 1)
            } else {
                $reportText += "`nAUDIT_MODEL: $auditModel`n"
            }
            [IO.File]::WriteAllText($reportPath, $reportText, [Text.UTF8Encoding]::new($false))
            $firstLine = Get-Content -LiteralPath $reportPath -TotalCount 1 -Encoding utf8
            $status = if ($Role -eq 'legacy' -and $firstLine -eq 'REVIEW_STATUS: complete') {
                'passed'
            } elseif ($Role -ne 'legacy' -and $firstLine -eq 'AUDIT_STATUS: complete') {
                'passed'
            } elseif ($Role -ne 'legacy' -and $firstLine -eq 'AUDIT_STATUS: deferred') {
                'deferred'
            } else {
                'failed (review incomplete)'
            }
        }
    }

    & $pythonExe $snapshotBuilder verify $snapshotPath $manifestPath | Out-Null
    if ($LASTEXITCODE -ne 0) {
        $status = 'failed (snapshot changed)'
    }

    if ($status -in @('passed', 'deferred')) {
        Copy-Item -LiteralPath $reportPath -Destination $latestPath -Force
    } else {
        $diagnostic = ''
        if (Test-Path -LiteralPath $stderrPath -PathType Leaf) {
            $tail = @(Get-Content -LiteralPath $stderrPath -Tail 8 -Encoding utf8 |
                ForEach-Object { $_.Substring(0, [Math]::Min($_.Length, 500)) }) -join "`n"
            $tail = $tail -replace 'sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}', '[REDACTED]'
            $tail = $tail -replace '(?i)(Bearer\s+)[A-Za-z0-9._~+/-]{20,}', '$1[REDACTED]'
            $diagnostic = "`nCLI stderr (last 8 redacted lines):`n$tail`n"
        }
        "# Design monitor: $status`n`nStarted: $($startedAt.ToString('o'))`n`nReport: $reportPath`n$diagnostic" |
            Set-Content -LiteralPath $latestPath -Encoding utf8
    }

    Write-Output "Design monitor $status. Latest result: $latestPath"
} finally {
    $cachePrefix = [IO.Path]::GetFullPath($outputDir).TrimEnd('\') + '\'
    $resolvedSnapshot = [IO.Path]::GetFullPath($snapshotPath)
    if (-not $resolvedSnapshot.StartsWith($cachePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing to clean a snapshot outside the monitor cache.'
    }
    if (Test-Path -LiteralPath $resolvedSnapshot -PathType Container) {
        Remove-Item -LiteralPath $resolvedSnapshot -Recurse -Force
    }
    Remove-Item -LiteralPath $packetPath, $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
}
if ($status -notin @('passed', 'deferred')) {
    exit 1
}
