param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).ProviderPath,
    [switch]$InstallPyInstaller,
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'

$projectPath = (Resolve-Path $ProjectRoot).ProviderPath
$pythonPath = Join-Path $projectPath '.venv\Scripts\python.exe'
$specPath = Join-Path $projectPath 'Stinger.spec'
$appDistName = 'SPS Calibration Stand'
$appExeName = 'SPS Calibration Stand.exe'
$distRoot = Join-Path $projectPath 'dist'
$distPath = Join-Path $distRoot $appDistName
$rootExePath = Join-Path $projectPath $appExeName
$workPath = Join-Path $projectPath 'build\pyinstaller'
$manifestPath = Join-Path $distPath 'build_manifest.json'

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
    & $pythonPath -m pytest -q tests/test_pressure_conversion.py tests/test_pressure_calibration.py
    & $pythonPath scripts\certify_ptp_applications.py --matrix docs\application_verification_matrix.csv --output logs\application_certification_build.csv --fail-on-blocked
}

# Remove prior output so stale onedir files cannot be published with the onefile exe.
if (Test-Path $distPath) {
    Remove-Item -Recurse -Force $distPath -ErrorAction SilentlyContinue
    if (Test-Path $distPath) { Start-Sleep -Seconds 2; Remove-Item -Recurse -Force $distPath -ErrorAction SilentlyContinue }
}
New-Item -ItemType Directory -Path $distPath -Force | Out-Null

# Note: --clean omitted to avoid PermissionError on network/synced build dirs.
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

$exePath = Join-Path $distPath $appExeName
if (-not (Test-Path $exePath)) {
    throw "Build succeeded but executable missing: $exePath"
}

Copy-Item $exePath $rootExePath -Force

# Bundle example config next to exe (fallback only; production uses STINGER_CONFIG_DIR).
$configSource = $null
if ($env:STINGER_CONFIG_DIR -and (Test-Path (Join-Path $env:STINGER_CONFIG_DIR 'stinger_config.yaml'))) {
    $configSource = Join-Path $env:STINGER_CONFIG_DIR 'stinger_config.yaml'
} else {
    $configSource = Join-Path $projectPath 'stinger_config.yaml'
}
if (Test-Path $configSource) {
    Copy-Item $configSource (Join-Path $distPath 'stinger_config.yaml') -Force
}

$gitCommit = ''
try {
    $gitCommit = (& git -C "$projectPath" rev-parse --short HEAD).Trim()
} catch {
    $gitCommit = ''
}

$manifest = [ordered]@{
    app_name = 'SPS Calibration Stand'
    artifact = $appExeName
    build_timestamp_utc = (Get-Date).ToUniversalTime().ToString('o')
    git_commit = $gitCommit
    source_spec = 'Stinger.spec'
}

$manifest | ConvertTo-Json -Depth 4 | Set-Content -Path $manifestPath -Encoding UTF8

# Publish EXE only to Z:\bin (do not MIR repo docs on Z: root)
$releaseRoot = 'Z:\Engineering\Program Builds\Python Builds\Stinger'
$binPath = Join-Path $releaseRoot 'bin'
if (Test-Path (Split-Path $releaseRoot -Parent)) {
    New-Item -ItemType Directory -Path $binPath -Force | Out-Null
    Copy-Item $exePath (Join-Path $binPath $appExeName) -Force
    Copy-Item $manifestPath (Join-Path $binPath 'stinger_build_manifest.json') -Force
    Write-Host "Published $appExeName to: $binPath"
}

Write-Host "Build complete: $exePath"
Write-Host "Root executable updated: $rootExePath"
Write-Host "Manifest: $manifestPath"
Write-Host "For full desktop deploy, run: .\scripts\deploy_build_and_install.ps1"
