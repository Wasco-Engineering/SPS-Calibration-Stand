# Set per-user environment variables for a Stinger stand.
# Usage:
#   .\scripts\deploy_set_stand_env.ps1 -StandId CA-SPS-02
#   .\scripts\deploy_set_stand_env.ps1 -StandId CA-SPS-02 -ConfigDir C:\Stinger\configs\CA-MAN-SPS-02
#
# Prefer omitting -ConfigDir so hostname auto-select (configs/<COMPUTERNAME>/) is used.
param(
    [Parameter(Mandatory = $true)]
    [string] $StandId,

    [string] $ConfigDir = ''
)

$ErrorActionPreference = 'Stop'

[System.Environment]::SetEnvironmentVariable('STINGER_STAND_ID', $StandId, 'User')
if ($ConfigDir) {
    $configDir = [System.IO.Path]::GetFullPath($ConfigDir)
    [System.Environment]::SetEnvironmentVariable('STINGER_CONFIG_DIR', $configDir, 'User')
    Write-Host 'Set User env:'
    Write-Host "  STINGER_STAND_ID=$StandId"
    Write-Host "  STINGER_CONFIG_DIR=$configDir"
} else {
    [System.Environment]::SetEnvironmentVariable('STINGER_CONFIG_DIR', $null, 'User')
    Write-Host 'Set User env:'
    Write-Host "  STINGER_STAND_ID=$StandId"
    Write-Host '  STINGER_CONFIG_DIR=(cleared — using configs/<hostname>/)'
}
Write-Host 'Restart terminal or sign out/in for apps to pick up new values.'
