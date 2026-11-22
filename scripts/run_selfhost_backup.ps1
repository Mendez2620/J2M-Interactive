param(
    [string]$Repository = (Resolve-Path "$PSScriptRoot\.."),
    [string]$KeyFile = "$env:LOCALAPPDATA\J2M\backup-key.dpapi"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $Repository
. "$PSScriptRoot\selfhost_task_runtime.ps1"
$failure = "selfhost-backups\logs\backup.failed"
$runtimeLog = "selfhost-backups\logs\task-runtime.jsonl"
$taskGuard = $null
$exitCode = 1
try {
    $taskGuard = Enter-J2MSelfHostTaskGuard
    Send-J2MHealthcheck database_hourly start
    Wait-J2MDockerReady -LogPath $runtimeLog
    $privateConfig = @{}
    foreach ($line in Get-Content -LiteralPath ".env.selfhost") {
        if ($line -match '^([A-Z0-9_]+)=(.*)$') { $privateConfig[$matches[1]] = $matches[2].Trim('"', "'") }
    }
    $mediaEnabled = $privateConfig.ContainsKey("J2M_MEDIA_BACKUP_ENABLED") -and $privateConfig["J2M_MEDIA_BACKUP_ENABLED"] -eq "true"
    if ($mediaEnabled) {
        if (-not $privateConfig.ContainsKey("J2M_IMAGE")) { throw "approved image pin unavailable" }
        python -m scripts.selfhost_backup integral --env-file .env.selfhost --media-volume j2m-cutover-ready_j2m-media --media-image $privateConfig["J2M_IMAGE"] --output-dir selfhost-backups/hourly --key-file $KeyFile --retries 3
    } else {
        python -m scripts.selfhost_backup database --env-file .env.selfhost --output-dir selfhost-backups/hourly --key-file $KeyFile --retries 3
    }
    if ($LASTEXITCODE -ne 0) { throw "database backup failed" }
    $configArgs = @("-m", "scripts.selfhost_backup", "configuration", "--path", ".env.selfhost", "--output-dir", "selfhost-backups/configuration", "--key-file", $KeyFile)
    if (Test-Path -LiteralPath ".env.cloudflared") { $configArgs += @("--path", ".env.cloudflared") }
    & python @configArgs
    if ($LASTEXITCODE -ne 0) { throw "configuration backup failed" }
    Remove-Item -LiteralPath $failure -Force -ErrorAction SilentlyContinue
    Send-J2MHealthcheck database_hourly success
    $exitCode = 0
} catch {
    New-Item -ItemType Directory -Force -Path (Split-Path $failure) | Out-Null
    Set-Content -LiteralPath $failure -Value ((Get-Date).ToUniversalTime().ToString("o") + " backup_failed") -Encoding ascii
    Send-J2MHealthcheck database_hourly fail
    $exitCode = 1
} finally {
    Exit-J2MSelfHostTaskGuard $taskGuard
}
exit $exitCode
