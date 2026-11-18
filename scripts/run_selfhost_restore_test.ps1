param(
    [string]$Repository = (Resolve-Path "$PSScriptRoot\.."),
    [string]$KeyFile = "$env:LOCALAPPDATA\J2M\backup-key.dpapi"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $Repository
$failure = "selfhost-backups\logs\restore.failed"
try {
    $latest = Get-ChildItem -LiteralPath "selfhost-backups\hourly" -Filter "*.complete.json" | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    if ($null -eq $latest) { throw "encrypted backup unavailable" }
    $backup = $latest.FullName.Substring(0, $latest.FullName.Length - ".complete.json".Length)
    python -m scripts.selfhost_backup restore-isolated $backup --key-file $KeyFile --lock-file "selfhost-backups\.operations.lock" --timeout 900
    if ($LASTEXITCODE -ne 0) { throw "isolated restore failed" }
    Remove-Item -LiteralPath $failure -Force -ErrorAction SilentlyContinue
    exit 0
} catch {
    New-Item -ItemType Directory -Force -Path (Split-Path $failure) | Out-Null
    Set-Content -LiteralPath $failure -Value ((Get-Date).ToUniversalTime().ToString("o") + " restore_failed") -Encoding ascii
    exit 1
}
