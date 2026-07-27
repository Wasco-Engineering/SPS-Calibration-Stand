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

$stingerSrc = Join-Path $repoRoot 'stinger_config.yaml'
$qualitySrc = Join-Path $repoRoot 'quality_cal_config.yaml'
$stingerDst = Join-Path $destRoot 'stinger_config.yaml'
$qualityDst = Join-Path $destRoot 'quality_cal_config.yaml'

if (-not $EquipmentId) { $EquipmentId = $StandId }

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

Copy-ConfigFile -Source $stingerSrc -Destination $stingerDst
Copy-ConfigFile -Source $qualitySrc -Destination $qualityDst

$content = Get-Content -Path $stingerDst -Raw
$content = $content -replace 'equipment_id:\s*".*"', "equipment_id: `"$EquipmentId`""
Set-Content -Path $stingerDst -Value $content -NoNewline

Write-Host ''
Write-Host "Install / config directory: $destRoot"
Write-Host ''
Write-Host 'Machine-wide (elevated):'
Write-Host "  .\scripts\deploy_set_machine_env.ps1 -StandId $StandId -ConfigDir `"$destRoot`""
Write-Host ''
Write-Host 'Per-user:'
Write-Host "  .\scripts\deploy_set_stand_env.ps1 -StandId $StandId -ConfigDir `"$destRoot`""
Write-Host ''
Write-Host 'Edit COM ports, DB credentials, and Mensor port in the YAML files above.'
