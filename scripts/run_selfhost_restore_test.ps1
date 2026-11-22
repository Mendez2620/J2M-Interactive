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
    $privateConfig = @{}
    foreach ($line in Get-Content -LiteralPath ".env.selfhost") {
        if ($line -match '^([A-Z0-9_]+)=(.*)$') { $privateConfig[$matches[1]] = $matches[2].Trim('"', "'") }
    }
    $mediaEnabled = $privateConfig.ContainsKey("J2M_MEDIA_BACKUP_ENABLED") -and $privateConfig["J2M_MEDIA_BACKUP_ENABLED"] -eq "true"
    $backupMode = if ($mediaEnabled) { "integral" } else { "legacy" }
    $selectedName = (& python -m scripts.selfhost_backup select-backup --source-dir "selfhost-backups\hourly" --key-file $KeyFile --mode $backupMode --max-age-seconds 90000 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($selectedName) -or $selectedName.Contains("\") -or $selectedName.Contains("/")) {
        throw "recent authenticated backup unavailable for configured mode"
    }
    $backup = Join-Path "selfhost-backups\hourly" $selectedName
    if ((Split-Path -Leaf $backup) -match '^j2m-set-([0-9a-f]{32})\.j2mbackup$') {
        $pin = Get-Content -LiteralPath ".env.selfhost" | Where-Object { $_ -match '^J2M_IMAGE=j2m-interactive:[0-9a-f]{40}$' } | Select-Object -First 1
        if ($null -eq $pin) { throw "approved image pin unavailable" }
        python -m scripts.selfhost_backup restore-integral $backup --image $pin.Substring("J2M_IMAGE=".Length) --key-file $KeyFile --lock-file "selfhost-backups\.operations.lock" --timeout 900
        if ($LASTEXITCODE -ne 0) { throw "isolated integral restore failed" }
    } else {
        python -m scripts.selfhost_backup restore-isolated $backup --key-file $KeyFile --lock-file "selfhost-backups\.operations.lock" --timeout 900
        if ($LASTEXITCODE -ne 0) { throw "isolated restore failed" }
    }
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
