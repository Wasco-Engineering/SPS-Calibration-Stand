# Set Machine-level Stinger env so all users (e.g. CalibrationUser) share stand identity.
#
# Hostname auto-selects configs/<COMPUTERNAME>/ from the repo. Do NOT set
# STINGER_CONFIG_DIR unless you intentionally override that behavior.
param(
    [Parameter(Mandatory = $true)]
    [string] $StandId,

    [string] $ConfigDir = '',

    [switch] $ClearConfigDir
)

$ErrorActionPreference = 'Stop'

try {
    [System.Environment]::SetEnvironmentVariable('STINGER_STAND_ID', $StandId, 'Machine')
    if ($ConfigDir) {
        $resolved = [System.IO.Path]::GetFullPath($ConfigDir)
        [System.Environment]::SetEnvironmentVariable('STINGER_CONFIG_DIR', $resolved, 'Machine')
        Write-Host "Machine env set:"
        Write-Host "  STINGER_STAND_ID=$StandId"
        Write-Host "  STINGER_CONFIG_DIR=$resolved"
    } else {
        # Clear legacy Machine STINGER_CONFIG_DIR so hostname configs win.
        [System.Environment]::SetEnvironmentVariable('STINGER_CONFIG_DIR', $null, 'Machine')
        Write-Host "Machine env set:"
        Write-Host "  STINGER_STAND_ID=$StandId"
        Write-Host "  STINGER_CONFIG_DIR=(cleared — using configs/<hostname>/)"
    }
} catch {
    throw "Failed to set Machine environment (run PowerShell as Administrator): $($_.Exception.Message)"
}

Write-Host "Requires Administrator. Users must sign out/in or reboot to pick up Machine env."
