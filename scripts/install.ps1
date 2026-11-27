[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$InstallerArgs
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Py) {
    $Py = (Get-Command py -ErrorAction SilentlyContinue).Source
    if ($Py) {
        & $Py -3 (Join-Path $ScriptDir "install.py") @InstallerArgs
        exit $LASTEXITCODE
    }
    Write-Error "Python >= 3.10 is required."
    exit 1
}

& $Py (Join-Path $ScriptDir "install.py") @InstallerArgs
exit $LASTEXITCODE
