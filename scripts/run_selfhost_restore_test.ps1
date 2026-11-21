param(
    [string]$Repository = (Resolve-Path "$PSScriptRoot\.."),
    [string]$KeyFile = "$env:LOCALAPPDATA\J2M\backup-key.dpapi"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $Repository
. "$PSScriptRoot\selfhost_task_runtime.ps1"
$failure = "selfhost-backups\logs\restore.failed"
$runtimeLog = "selfhost-backups\logs\task-runtime.jsonl"
$taskGuard = $null
$exitCode = 1
try {
    $taskGuard = Enter-J2MSelfHostTaskGuard
    Send-J2MHealthcheck restore_weekly start
    Wait-J2MDockerReady -LogPath $runtimeLog
    $latest = Get-ChildItem -LiteralPath "selfhost-backups\hourly" -Filter "*.complete.json" | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    if ($null -eq $latest) { throw "encrypted backup unavailable" }
    $backup = $latest.FullName.Substring(0, $latest.FullName.Length - ".complete.json".Length)
    python -m scripts.selfhost_backup restore-isolated $backup --key-file $KeyFile --lock-file "selfhost-backups\.operations.lock" --timeout 900
    if ($LASTEXITCODE -ne 0) { throw "isolated restore failed" }
    Remove-Item -LiteralPath $failure -Force -ErrorAction SilentlyContinue
    Send-J2MHealthcheck restore_weekly success
    $exitCode = 0
} catch {
    New-Item -ItemType Directory -Force -Path (Split-Path $failure) | Out-Null
    Set-Content -LiteralPath $failure -Value ((Get-Date).ToUniversalTime().ToString("o") + " restore_failed") -Encoding ascii
    Send-J2MHealthcheck restore_weekly fail
    $exitCode = 1
} finally {
    Exit-J2MSelfHostTaskGuard $taskGuard
}
exit $exitCode
