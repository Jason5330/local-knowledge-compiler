param(
    [Parameter(Mandatory = $true)]
    [string]$Vault
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Missing .venv\Scripts\python.exe. Follow README: Initial setup."
}

if (-not (Test-Path -LiteralPath $Vault -PathType Container)) {
    throw "Vault directory not found: $Vault"
}

& $Python -m local_kb.cli watch $Vault
if ($LASTEXITCODE -ne 0) {
    throw "Knowledge watcher stopped with exit code: $LASTEXITCODE"
}
