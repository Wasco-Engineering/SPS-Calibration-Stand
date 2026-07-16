# Build Stinger + QualityCal and install to C:\Stinger (and optional Z:\bin).
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).ProviderPath,
    [switch]$InstallPyInstaller,
    [switch]$SkipTests,
    [string]$StandId = 'CA-SPS-01',
    [string]$InstallRoot = 'C:\Stinger',
    [string]$TargetUser = 'CalibrationUser',
    [switch]$SetMachineEnv,
    [switch]$DesktopShortcuts,
    [switch]$SkipCalibrationUserDesktop
)

$ErrorActionPreference = 'Stop'
$projectPath = (Resolve-Path $ProjectRoot).ProviderPath

$installArgs = @{
    ProjectRoot       = $projectPath
    InstallRoot       = $InstallRoot
    StandId           = $StandId
    Build             = $true
    TargetUser        = $TargetUser
}
if ($InstallPyInstaller) { $installArgs['InstallPyInstaller'] = $true }
if ($SkipTests) { $installArgs['SkipTests'] = $true }
if ($SetMachineEnv) { $installArgs['SetMachineEnv'] = $true }
if ($DesktopShortcuts -or -not $SkipCalibrationUserDesktop) {
    $installArgs['DesktopShortcuts'] = $true
}

& (Join-Path $projectPath 'scripts\deploy_install_to_c_stinger.ps1') @installArgs
