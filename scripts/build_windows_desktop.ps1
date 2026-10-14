param(
    [switch]$SkipFrontendBuild,
    [string]$OutputRoot,
    [string]$WorkRoot
)

$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$frontendRoot = Join-Path $repositoryRoot 'frontend'
$python = Join-Path $repositoryRoot '.venv\Scripts\python.exe'
$distDirectory = Join-Path $frontendRoot 'dist'

function Resolve-RepositoryPath([string]$PathValue, [string]$DefaultRelative) {
    if ([string]::IsNullOrWhiteSpace($PathValue)) {
        $PathValue = $DefaultRelative
    }
    if (-not [System.IO.Path]::IsPathRooted($PathValue)) {
        $PathValue = Join-Path $repositoryRoot $PathValue
    }
    $resolved = [System.IO.Path]::GetFullPath($PathValue)
    $rootPrefix = [System.IO.Path]::GetFullPath($repositoryRoot).TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Build output and work paths must remain inside the repository.'
    }
    return $resolved
}

$outputRoot = Resolve-RepositoryPath $OutputRoot 'release\windows'
$workRoot = Resolve-RepositoryPath $WorkRoot 'build\pyinstaller'
$protectedReleaseRoot = [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot 'packaging\release'))
$protectedPrefix = $protectedReleaseRoot.TrimEnd('\') + '\'
$outputTouchesRelease = $outputRoot.Equals($protectedReleaseRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
    $outputRoot.StartsWith($protectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)
$workTouchesRelease = $workRoot.Equals($protectedReleaseRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
    $workRoot.StartsWith($protectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)
if ($outputTouchesRelease -or $workTouchesRelease) {
    throw 'The packaging\release deliverables are protected build outputs.'
}
$outputPrefix = $outputRoot.TrimEnd('\') + '\'
$workPrefix = $workRoot.TrimEnd('\') + '\'
$buildPathsOverlap = $outputRoot.Equals($workRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
    $outputRoot.StartsWith($workPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or
    $workRoot.StartsWith($outputPrefix, [System.StringComparison]::OrdinalIgnoreCase)
if ($buildPathsOverlap) {
    throw 'Build output and PyInstaller work paths must not overlap.'
}

if (-not (Test-Path -LiteralPath $python)) {
    throw 'Expected .venv\Scripts\python.exe. Create the audited build environment first.'
}

if (-not $SkipFrontendBuild) {
    Push-Location $frontendRoot
    try {
        npm.cmd run build
    }
    finally {
        Pop-Location
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $distDirectory 'index.html'))) {
    throw 'frontend\dist\index.html is missing. Build the frontend before packaging.'
}

& $python -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) {
    throw 'PyInstaller is required only for packaging. Install the approved packaging dependency before running this script.'
}

$stagingDirectory = Join-Path $outputRoot 'YuragiStriderBackend'
$applicationDirectory = Join-Path $outputRoot 'Yuragi-Strider'
$launcherSource = Join-Path $repositoryRoot 'packaging\windows\YuragiStriderLauncher.cs'
$applicationIcon = Join-Path $repositoryRoot 'packaging\windows\yuragi-strider.ico'
if (-not (Test-Path -LiteralPath $applicationIcon)) {
    throw "The application icon is missing: $applicationIcon"
}

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --console `
    --name YuragiStriderBackend `
    --icon $applicationIcon `
    --distpath $outputRoot `
    --workpath $workRoot `
    --specpath $workRoot `
    --add-data "$distDirectory;frontend\dist" `
    --collect-submodules api `
    --collect-submodules core `
    --hidden-import uvicorn.protocols.websockets.websockets_impl `
    --collect-submodules websockets `
    (Join-Path $repositoryRoot 'desktop_app.py')

New-Item -ItemType Directory -Path $applicationDirectory -Force *> $null
Get-ChildItem -LiteralPath $stagingDirectory -Force |
    Copy-Item -Destination $applicationDirectory -Recurse -Force

$csharpCompiler = Get-Command csc.exe -ErrorAction SilentlyContinue
if ($null -eq $csharpCompiler) {
    $legacyCompiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    if (-not (Test-Path -LiteralPath $legacyCompiler)) {
        throw 'The Windows C# compiler (csc.exe) is required to build the GUI launcher.'
    }
    $csharpCompilerPath = $legacyCompiler
}
else {
    $csharpCompilerPath = $csharpCompiler.Source
}

$launcherOutput = Join-Path $applicationDirectory 'Yuragi-Strider.exe'
$compilerArguments = @(
    '/nologo',
    '/target:winexe',
    "/out:$launcherOutput",
    '/r:System.Windows.Forms.dll',
    '/r:System.Drawing.dll',
    "/win32icon:$applicationIcon",
    $launcherSource
)

& $csharpCompilerPath @compilerArguments
if ($LASTEXITCODE -ne 0) {
    throw 'The Yuragi-Strider GUI launcher could not be compiled.'
}

$shortcutPath = Join-Path $applicationDirectory 'Yuragi-Strider.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $applicationDirectory 'Yuragi-Strider.exe'
$shortcut.WorkingDirectory = $applicationDirectory
$shortcut.IconLocation = Join-Path $applicationDirectory 'Yuragi-Strider.exe'
$shortcut.Description = 'Start Yuragi-Strider.'
$shortcut.Save()

Write-Host "Built: $(Join-Path $applicationDirectory 'Yuragi-Strider.exe')"
