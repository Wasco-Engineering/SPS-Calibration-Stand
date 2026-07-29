# Initialize per-PC Stinger config under the install root (default C:\Stinger).
#
# Usage:
#   .\scripts\deploy_init_stand.ps1 -StandId CA-SPS-01 -EquipmentId CA-SPS-01
#   .\scripts\deploy_init_stand.ps1 -InstallRoot C:\Stinger -Force
#
param(
    [string] $StandId = 'CA-SPS-01',

    [string] $EquipmentId = '',
    [string] $InstallRoot = 'C:\Stinger',
    [string] $RepoRoot = '',
    [switch] $Force
)

$ErrorActionPreference = 'Stop'
if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$repoRoot = [System.IO.Path]::GetFullPath($RepoRoot)
$destRoot = [System.IO.Path]::GetFullPath($InstallRoot)

New-Item -ItemType Directory -Path $destRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $destRoot 'logs') -Force | Out-Null

if (-not $EquipmentId) { $EquipmentId = $StandId }

# Prefer repo configs/<hostname>/ (or configs matched by stand id).
$hostname = $env:COMPUTERNAME
$configsRoot = Join-Path $repoRoot 'configs'
$hostDir = Join-Path $configsRoot $hostname
if (-not (Test-Path (Join-Path $hostDir 'stinger_config.yaml'))) {
    foreach ($dir in Get-ChildItem -Path $configsRoot -Directory -ErrorAction SilentlyContinue) {
        $cfgPath = Join-Path $dir.FullName 'stinger_config.yaml'
        if (-not (Test-Path $cfgPath)) { continue }
        if (Select-String -Path $cfgPath -Pattern "equipment_id:\s*$EquipmentId\b" -Quiet) {
            $hostDir = $dir.FullName
            break
        }
    }
}

$stingerDst = Join-Path $hostDir 'stinger_config.yaml'
$qualityDst = Join-Path $hostDir 'quality_cal_config.yaml'

if (-not (Test-Path $hostDir)) {
    New-Item -ItemType Directory -Path $hostDir -Force | Out-Null
}

function Copy-ConfigFile {
    param([string] $Source, [string] $Destination)
    if ((Test-Path $Destination) -and -not $Force) {
        Write-Host "SKIP (exists): $Destination"
        return
    }
    if (-not (Test-Path $Source)) {
        throw "Missing template: $Source"
    }
    Copy-Item -Path $Source -Destination $Destination -Force
    Write-Host "Wrote: $Destination"
}

# Seed from another stand template only when this host folder is empty.
$templateHost = Join-Path $configsRoot 'CA-MAN-SPS-02'
if (-not (Test-Path $stingerDst)) {
    $stingerSrc = Join-Path $templateHost 'stinger_config.yaml'
    if (-not (Test-Path $stingerSrc)) {
        $stingerSrc = Join-Path $repoRoot 'stinger_config.yaml'
    }
    Copy-ConfigFile -Source $stingerSrc -Destination $stingerDst
}
if (-not (Test-Path $qualityDst)) {
    $qualitySrc = Join-Path $templateHost 'quality_cal_config.yaml'
    if (-not (Test-Path $qualitySrc)) {
        $qualitySrc = Join-Path $repoRoot 'quality_cal_config.yaml'
    }
    Copy-ConfigFile -Source $qualitySrc -Destination $qualityDst
}

if (Test-Path $stingerDst) {
    $content = Get-Content -Path $stingerDst -Raw
    $content = $content -replace 'equipment_id:\s*\S+', "equipment_id: $EquipmentId"
    Set-Content -Path $stingerDst -Value $content -NoNewline
}

Write-Host ''
Write-Host "Host config directory: $hostDir"
Write-Host "Hostname:              $hostname"
Write-Host "Stand / equipment:     $StandId / $EquipmentId"
Write-Host ''
Write-Host 'Machine-wide (elevated) — prefer hostname auto-select (no STINGER_CONFIG_DIR):'
Write-Host "  .\scripts\deploy_set_machine_env.ps1 -StandId $StandId"
Write-Host ''
Write-Host 'Edit COM ports, DB credentials, error models, and Mensor port in:'
Write-Host "  $stingerDst"
