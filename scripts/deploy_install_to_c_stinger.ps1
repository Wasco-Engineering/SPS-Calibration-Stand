# Install built Stinger apps under C:\Stinger (standard stand layout).
#
# Per-computer YAML lives in repo configs/<hostname>/ and is auto-selected at runtime.
#
# Usage (from repo, after build or with -Build):
#   .\scripts\deploy_install_to_c_stinger.ps1 -StandId CA-SPS-02 -SetMachineEnv
#   .\scripts\deploy_install_to_c_stinger.ps1 -Build -InstallPyInstaller -SetMachineEnv
#
# Run -SetMachineEnv in an elevated shell so CalibrationUser picks up STINGER_STAND_ID
# (do not set STINGER_CONFIG_DIR — hostname config selection needs it cleared).
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).ProviderPath,
    [string]$InstallRoot = 'C:\Stinger',
    [string]$StandId = 'CA-SPS-01',
    [string]$EquipmentId = '',
    [string]$TargetUser = 'CalibrationUser',
    [switch]$Build,
    [switch]$InstallPyInstaller,
    [switch]$SkipTests,
    [switch]$SetMachineEnv,
    [switch]$ForceConfig,
    [switch]$DesktopShortcuts,
    [switch]$SkipZBin
)

$ErrorActionPreference = 'Stop'
$projectPath = (Resolve-Path $ProjectRoot).ProviderPath
$installPath = [System.IO.Path]::GetFullPath($InstallRoot)

if (-not $EquipmentId) { $EquipmentId = $StandId }

New-Item -ItemType Directory -Path $installPath -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $installPath 'logs') -Force | Out-Null
$templatesSrc = Join-Path $projectPath 'deploy\templates'
$templatesDest = Join-Path $installPath 'deploy\templates'
$sameRoot = (
    [System.IO.Path]::GetFullPath($projectPath).TrimEnd('\') -eq
    [System.IO.Path]::GetFullPath($installPath).TrimEnd('\')
)
if ((Test-Path $templatesSrc) -and -not $sameRoot) {
    New-Item -ItemType Directory -Path $templatesDest -Force | Out-Null
    Copy-Item (Join-Path $templatesSrc '*') $templatesDest -Recurse -Force
}

$configsSrc = Join-Path $projectPath 'configs'
$configsDest = Join-Path $installPath 'configs'
if ((Test-Path $configsSrc) -and -not $sameRoot) {
    New-Item -ItemType Directory -Path $configsDest -Force | Out-Null
    Copy-Item (Join-Path $configsSrc '*') $configsDest -Recurse -Force
    Write-Host "Copied configs/ -> $configsDest"
}

$registrySrc = Join-Path $projectPath 'deploy\DEPLOYMENT_REGISTRY.yaml'
$registryDestDir = Join-Path $installPath 'deploy'
if ((Test-Path $registrySrc) -and -not $sameRoot) {
    New-Item -ItemType Directory -Path $registryDestDir -Force | Out-Null
    Copy-Item $registrySrc $registryDestDir -Force
}

$initArgs = @{
    StandId       = $StandId
    EquipmentId   = $EquipmentId
    InstallRoot   = $installPath
}
if ($ForceConfig) { $initArgs['Force'] = $true }
& (Join-Path $projectPath 'scripts\deploy_init_stand.ps1') @initArgs

if ($SetMachineEnv) {
    try {
        & (Join-Path $projectPath 'scripts\deploy_set_machine_env.ps1') -StandId $StandId -ConfigDir $installPath
    } catch {
        Write-Warning "Machine env not set (run elevated for all users): $_"
        Write-Warning 'Setting per-user env for the current account instead.'
        & (Join-Path $projectPath 'scripts\deploy_set_stand_env.ps1') -StandId $StandId -ConfigDir $installPath
    }
} else {
    & (Join-Path $projectPath 'scripts\deploy_set_stand_env.ps1') -StandId $StandId -ConfigDir $installPath
}

$pythonPath = Join-Path $projectPath '.venv\Scripts\python.exe'
if (-not (Test-Path $pythonPath)) {
    throw "Missing virtualenv: $pythonPath (create .venv before -Build)"
}

$env:STINGER_STAND_ID = $StandId
$env:STINGER_CONFIG_DIR = $installPath

if ($Build) {
    & $pythonPath (Join-Path $projectPath 'scripts\bootstrap_qf87_template.py')
    $buildArgs = @{ ProjectRoot = $projectPath }
    if ($InstallPyInstaller) { $buildArgs['InstallPyInstaller'] = $true }
    if ($SkipTests) { $buildArgs['SkipTests'] = $true }
    & (Join-Path $projectPath 'scripts\build_stinger.ps1') @buildArgs
    & (Join-Path $projectPath 'scripts\build_quality_cal.ps1') @buildArgs
}

$stingerExe = 'SPS Calibration Stand.exe'
foreach ($pair in @(
    @{ Sub = 'SPS Calibration Stand'; Exe = $stingerExe },
    @{ Sub = 'QualityCal'; Exe = 'QualityCal.exe' }
)) {
    $src = Join-Path $projectPath "dist\$($pair.Sub)\$($pair.Exe)"
    if (-not (Test-Path $src)) {
        Write-Warning "Missing build output (run with -Build): $src"
        continue
    }
    Copy-Item $src (Join-Path $installPath $pair.Exe) -Force
    Write-Host "Installed: $(Join-Path $installPath $pair.Exe)"
}

$legacyStinger = Join-Path $installPath 'Stinger.exe'
if (Test-Path $legacyStinger) {
    Remove-Item $legacyStinger -Force
    Write-Host "Removed legacy: $legacyStinger"
}

if (-not $SkipZBin) {
    $binDir = 'Z:\Engineering\Program Builds\Python Builds\Stinger\bin'
    if (Test-Path (Split-Path $binDir -Parent)) {
        New-Item -ItemType Directory -Path $binDir -Force | Out-Null
        foreach ($pair in @($stingerExe, 'QualityCal.exe')) {
            $built = Join-Path $installPath $pair
            if (Test-Path $built) {
                Copy-Item $built (Join-Path $binDir $pair) -Force
            }
        }
        Write-Host "Published to Z: $binDir"
    }
}

if ($DesktopShortcuts) {
    $targets = @(
        @{ Name = 'SPS Calibration Stand.lnk'; Exe = $stingerExe },
        @{ Name = 'Quality Calibration.lnk'; Exe = 'QualityCal.exe' }
    )
    if ($TargetUser -eq $env:USERNAME) {
        $desktop = [Environment]::GetFolderPath('Desktop')
    } else {
        $desktop = "C:\Users\$TargetUser\Desktop"
    }
    $WshShell = New-Object -ComObject WScript.Shell
    foreach ($item in $targets) {
        $exe = Join-Path $installPath $item.Exe
        if (-not (Test-Path $exe)) { continue }
        $lnk = Join-Path $desktop $item.Name
        $sc = $WshShell.CreateShortcut($lnk)
        $sc.TargetPath = $exe
        $sc.WorkingDirectory = $installPath
        $sc.Save()
        Write-Host "Shortcut: $lnk"
    }
    $legacyLnk = Join-Path $desktop 'Stinger.lnk'
    if (Test-Path $legacyLnk) {
        Remove-Item $legacyLnk -Force
        Write-Host "Removed legacy shortcut: $legacyLnk"
    }
}

$manifest = [ordered]@{
    build_timestamp_utc = (Get-Date).ToUniversalTime().ToString('o')
    hostname            = $env:COMPUTERNAME
    stand_id            = $StandId
    install_root        = $installPath
    config_dir          = $installPath
    artifacts           = @($stingerExe, 'QualityCal.exe')
}
$manifest | ConvertTo-Json | Set-Content (Join-Path $installPath 'install_manifest.json') -Encoding UTF8

Write-Host ''
Write-Host "Install root:  $installPath"
Write-Host "Configs:       $(Join-Path $installPath 'stinger_config.yaml')"
Write-Host "               $(Join-Path $installPath 'quality_cal_config.yaml')"
if ($SetMachineEnv) {
    Write-Host 'Machine env set — sign out/in or reboot for all users (e.g. CalibrationUser).'
}
if ($DesktopShortcuts -and $TargetUser -ne $env:USERNAME) {
    Write-Host 'If shortcuts are missing, re-run with an elevated PowerShell.'
}
