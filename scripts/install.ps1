$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $Uv) {
    throw 'uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/ and rerun this script.'
}

& $Uv.Source tool install --upgrade --force $RepoRoot
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "Installed agent-goechala. Run: agent-goechala"
