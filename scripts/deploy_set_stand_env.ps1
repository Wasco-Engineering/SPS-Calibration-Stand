# Set per-user environment variables for a Stinger stand.
# Usage: .\scripts\deploy_set_stand_env.ps1 -StandId CA-SPS-01 [-ConfigDir C:\Stinger]
param(
    [Parameter(Mandatory = $true)]
    [string] $StandId,

    [string] $ConfigDir = ''
)

$ErrorActionPreference = 'Stop'
if (-not $ConfigDir) {
    $ConfigDir = 'C:\Stinger'
}
$configDir = [System.IO.Path]::GetFullPath($ConfigDir)

[System.Environment]::SetEnvironmentVariable('STINGER_STAND_ID', $StandId, 'User')
[System.Environment]::SetEnvironmentVariable('STINGER_CONFIG_DIR', $configDir, 'User')

Write-Host 'Set User env:'
Write-Host "  STINGER_STAND_ID=$StandId"
Write-Host "  STINGER_CONFIG_DIR=$configDir"
Write-Host 'Restart terminal or sign out/in for apps to pick up new values.'
