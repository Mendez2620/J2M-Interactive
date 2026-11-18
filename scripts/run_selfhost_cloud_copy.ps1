param(
    [Parameter(Mandatory = $true)][string]$Remote,
    [string]$Repository = (Resolve-Path "$PSScriptRoot\.."),
    [string]$KeyFile = "$env:LOCALAPPDATA\J2M\backup-key.dpapi"
)

$ErrorActionPreference = "Stop"
$failure = Join-Path $Repository "selfhost-backups\logs\cloud.failed"
$verifyDir = $null
try {
    if (-not (Get-Command rclone -ErrorAction SilentlyContinue)) { throw "rclone unavailable" }
    Set-Location -LiteralPath $Repository
    $root = Join-Path $Repository "selfhost-backups"
    $source = Join-Path $root "hourly"
    $outbound = Join-Path $root "outbound\four-hour"
    $verifyDir = Join-Path $root ("remote-verify-" + [guid]::NewGuid().ToString("N"))
    python -m scripts.selfhost_backup stage-offsite --source-dir $source --destination-dir $outbound --key-file $KeyFile --lock-file (Join-Path $root ".operations.lock")
    if ($LASTEXITCODE -ne 0) { throw "offsite staging failed" }
    $selectedItems = @(Get-ChildItem -LiteralPath $outbound -Filter "*.j2mbackup")
    if ($selectedItems.Count -ne 1) { throw "offsite staging did not produce exactly one backup" }
    $selected = $selectedItems[0]

    # Only one complete, locally authenticated bundle is staged for this four-hour slot.
    rclone copy $outbound "$Remote/four-hour" --immutable --retries 3 --contimeout 15s --timeout 5m
    if ($LASTEXITCODE -ne 0) { throw "cloud copy failed" }
    New-Item -ItemType Directory -Force -Path $verifyDir | Out-Null
    rclone copy "$Remote/four-hour" $verifyDir --include ($selected.Name + "*") --retries 3 --contimeout 15s --timeout 5m
    if ($LASTEXITCODE -ne 0) { throw "remote verification download failed" }
    python -m scripts.selfhost_backup verify (Join-Path $verifyDir $selected.Name) --key-file $KeyFile
    if ($LASTEXITCODE -ne 0) { throw "remote cryptographic verification failed" }

    $configuration = Join-Path $root "configuration"
    $configSelected = $null
    if (Test-Path -LiteralPath $configuration) {
        $configOutbound = Join-Path $root "outbound\configuration"
        python -m scripts.selfhost_backup stage-offsite --source-dir $configuration --destination-dir $configOutbound --key-file $KeyFile --lock-file (Join-Path $root ".operations.lock")
        if ($LASTEXITCODE -ne 0) { throw "configuration staging failed" }
        $configItems = @(Get-ChildItem -LiteralPath $configOutbound -Filter "*.j2mbackup")
        if ($configItems.Count -ne 1) { throw "configuration staging did not produce exactly one backup" }
        $configSelected = $configItems[0]
        rclone copy $configOutbound "$Remote/configuration" --immutable --retries 3 --contimeout 15s --timeout 5m
        if ($LASTEXITCODE -ne 0) { throw "configuration copy failed" }
        rclone copy "$Remote/configuration" $verifyDir --include ($configSelected.Name + "*") --retries 3 --contimeout 15s --timeout 5m
        if ($LASTEXITCODE -ne 0) { throw "configuration verification download failed" }
        python -m scripts.selfhost_backup verify (Join-Path $verifyDir $configSelected.Name) --key-file $KeyFile
        if ($LASTEXITCODE -ne 0) { throw "configuration cryptographic verification failed" }
    }

    if ((Get-Date).ToUniversalTime().Hour -lt 4) {
        foreach ($suffix in @("", ".sha256", ".complete.json")) {
            rclone copyto ($selected.FullName + $suffix) "$Remote/daily/$($selected.Name)$suffix" --immutable --retries 3 --contimeout 15s --timeout 5m
            if ($LASTEXITCODE -ne 0) { throw "daily copy failed" }
        }
        $dailyVerify = Join-Path $verifyDir "daily"
        New-Item -ItemType Directory -Force -Path $dailyVerify | Out-Null
        rclone copy "$Remote/daily" $dailyVerify --include ($selected.Name + "*") --retries 3 --contimeout 15s --timeout 5m
        python -m scripts.selfhost_backup verify (Join-Path $dailyVerify $selected.Name) --key-file $KeyFile
        if ($LASTEXITCODE -ne 0) { throw "daily cryptographic verification failed" }
    }

    # Retention never removes the current complete bundle, even if it is old.
    $exclude = @("--exclude", $selected.Name, "--exclude", ($selected.Name + ".sha256"), "--exclude", ($selected.Name + ".complete.json"))
    rclone delete "$Remote/four-hour" --min-age 7d --include "*.j2mbackup" --include "*.sha256" --include "*.complete.json" @exclude --retries 3
    if ($LASTEXITCODE -ne 0) { throw "four-hour retention failed" }
    rclone delete "$Remote/daily" --min-age 30d --include "*.j2mbackup" --include "*.sha256" --include "*.complete.json" @exclude --retries 3
    if ($LASTEXITCODE -ne 0) { throw "daily retention failed" }
    if ($null -ne $configSelected) {
        $configExclude = @("--exclude", $configSelected.Name, "--exclude", ($configSelected.Name + ".sha256"), "--exclude", ($configSelected.Name + ".complete.json"))
        rclone delete "$Remote/configuration" --min-age 30d --include "*.j2mbackup" --include "*.sha256" --include "*.complete.json" @configExclude --retries 3
        if ($LASTEXITCODE -ne 0) { throw "configuration retention failed" }
    }
    Remove-Item -LiteralPath $verifyDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $failure -Force -ErrorAction SilentlyContinue
    exit 0
} catch {
    if ($null -ne $verifyDir) { Remove-Item -LiteralPath $verifyDir -Recurse -Force -ErrorAction SilentlyContinue }
    New-Item -ItemType Directory -Force -Path (Split-Path $failure) | Out-Null
    Set-Content -LiteralPath $failure -Value ((Get-Date).ToUniversalTime().ToString("o") + " cloud_copy_failed") -Encoding ascii
    exit 1
}
