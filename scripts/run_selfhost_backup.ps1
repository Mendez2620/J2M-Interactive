param(
    [string]$Repository = (Resolve-Path "$PSScriptRoot\.."),
    [string]$KeyFile = "$env:LOCALAPPDATA\J2M\backup-key.dpapi"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $Repository
$failure = "selfhost-backups\logs\backup.failed"
try {
    python -m scripts.selfhost_backup database --env-file .env.selfhost --output-dir selfhost-backups/hourly --key-file $KeyFile --retries 3
    if ($LASTEXITCODE -ne 0) { throw "database backup failed" }
    python -m scripts.selfhost_backup retain-local selfhost-backups/hourly --keep 48
    if ($LASTEXITCODE -ne 0) { throw "local retention failed" }
    $configArgs = @("-m", "scripts.selfhost_backup", "configuration", "--path", ".env.selfhost", "--output-dir", "selfhost-backups/configuration", "--key-file", $KeyFile)
    if (Test-Path -LiteralPath ".env.cloudflared") { $configArgs += @("--path", ".env.cloudflared") }
    & python @configArgs
    if ($LASTEXITCODE -ne 0) { throw "configuration backup failed" }
    Remove-Item -LiteralPath $failure -Force -ErrorAction SilentlyContinue
    exit 0
} catch {
    New-Item -ItemType Directory -Force -Path (Split-Path $failure) | Out-Null
    Set-Content -LiteralPath $failure -Value ((Get-Date).ToUniversalTime().ToString("o") + " backup_failed") -Encoding ascii
    exit 1
}
