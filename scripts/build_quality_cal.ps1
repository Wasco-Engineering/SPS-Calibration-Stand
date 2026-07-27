param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).ProviderPath,
    [switch]$InstallPyInstaller,
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'

$projectPath = (Resolve-Path $ProjectRoot).ProviderPath
$pythonPath = Join-Path $projectPath '.venv\Scripts\python.exe'
$specPath = Join-Path $projectPath 'QualityCal.spec'
$distRoot = Join-Path $projectPath 'dist'
$distPath = Join-Path $distRoot 'QualityCal'
$workPath = Join-Path $projectPath 'build\pyinstaller_quality_cal'
$binPath = 'Z:\Engineering\Program Builds\Python Builds\Stinger\bin'

if (-not (Test-Path $pythonPath)) {
    throw "Missing virtualenv Python: $pythonPath"
}

if (-not (Test-Path $specPath)) {
    throw "Missing PyInstaller spec file: $specPath"
}

if ($InstallPyInstaller) {
    & $pythonPath -m pip install pyinstaller
}

if (-not $SkipTests) {
    & $pythonPath -m pytest -q tests/test_quality_cal_config.py tests/test_quality_cal_report.py tests/test_quality_cal_leak.py
}

if (Test-Path $distPath) {
    Remove-Item -Recurse -Force $distPath -ErrorAction SilentlyContinue
    if (Test-Path $distPath) { Start-Sleep -Seconds 2; Remove-Item -Recurse -Force $distPath -ErrorAction SilentlyContinue }
}
New-Item -ItemType Directory -Path $distPath -Force | Out-Null

# PyInstaller writes INFO to stderr; with $ErrorActionPreference=Stop that becomes a
# terminating NativeCommandError even when the build succeeds.
$prevEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $pythonPath -m PyInstaller --noconfirm --distpath "$distPath" --workpath "$workPath" "$specPath"
$pyExit = $LASTEXITCODE
$ErrorActionPreference = $prevEap
if ($pyExit -ne 0) {
    throw "PyInstaller failed with exit code $pyExit"
}

$exePath = Join-Path $distPath 'QualityCal.exe'
if (-not (Test-Path $exePath)) {
    throw "Build succeeded but executable missing: $exePath"
}

$configSource = $null
if ($env:STINGER_CONFIG_DIR -and (Test-Path (Join-Path $env:STINGER_CONFIG_DIR 'quality_cal_config.yaml'))) {
    $configSource = Join-Path $env:STINGER_CONFIG_DIR 'quality_cal_config.yaml'
} else {
    $configSource = Join-Path $projectPath 'quality_cal_config.yaml'
}
if (Test-Path $configSource) {
    Copy-Item $configSource (Join-Path $distPath 'quality_cal_config.yaml') -Force
}

if (Test-Path (Split-Path $binPath -Parent)) {
    New-Item -ItemType Directory -Path $binPath -Force | Out-Null
    Copy-Item $exePath (Join-Path $binPath 'QualityCal.exe') -Force
    Write-Host "Published QualityCal.exe to: $binPath"
}

Write-Host "Build complete: $exePath"
