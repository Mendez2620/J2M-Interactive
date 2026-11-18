param(
    [string]$Repository = (Resolve-Path "$PSScriptRoot\.."),
    [string]$KeyFile = "$env:LOCALAPPDATA\J2M\backup-key.dpapi"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $Repository
$failure = "selfhost-backups\logs\restore.failed"
try {
    $latest = Get-ChildItem -LiteralPath "selfhost-backups\hourly" -Filter "*.j2mbackup" | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    if ($null -eq $latest) { throw "encrypted backup unavailable" }
    python -m scripts.selfhost_backup restore-isolated $latest.FullName --key-file $KeyFile
    if ($LASTEXITCODE -ne 0) { throw "isolated restore failed" }
    Remove-Item -LiteralPath $failure -Force -ErrorAction SilentlyContinue
    exit 0
} catch {
    New-Item -ItemType Directory -Force -Path (Split-Path $failure) | Out-Null
    Set-Content -LiteralPath $failure -Value ((Get-Date).ToUniversalTime().ToString("o") + " restore_failed") -Encoding ascii
    exit 1
}
