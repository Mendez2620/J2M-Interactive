param(
    [Parameter(Mandatory = $true)][string]$Remote,
    [string]$Repository = (Resolve-Path "$PSScriptRoot\.."),
    [string]$KeyFile = "$env:LOCALAPPDATA\J2M\backup-key.dpapi"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $Repository
. "$PSScriptRoot\selfhost_task_runtime.ps1"
$failure = Join-Path $Repository "selfhost-backups\logs\cloud.failed"
$verifyDir = $null
$taskGuard = $null
$exitCode = 1

function Invoke-ExactRetention([string]$Category, [int]$Days) {
    $inventoryPath = Join-Path $verifyDir ($Category + "-inventory.json")
    $inventoryJson = (& rclone lsjson "$Remote/$Category" --files-only --no-mimetype | Out-String)
    if ($LASTEXITCODE -ne 0) { throw "$Category inventory failed" }
    Set-Content -LiteralPath $inventoryPath -Value $inventoryJson -Encoding utf8
    $planJson = (& python -m scripts.selfhost_backup retention-plan --inventory $inventoryPath --max-age-days $Days | Out-String)
    if ($LASTEXITCODE -ne 0) { throw "$Category retention planning failed" }
    $plan = $planJson | ConvertFrom-Json
    if ($null -eq $plan.protected) {
        if (@($plan.delete).Count -ne 0) { throw "$Category plan is not fail-closed" }
        return
    }
    $categoryVerify = Join-Path $verifyDir ("retention-" + $Category)
    New-Item -ItemType Directory -Force -Path $categoryVerify | Out-Null
    $protectedList = Join-Path $verifyDir ("protected-" + $Category + ".txt")
    @($plan.protected_files) | Set-Content -LiteralPath $protectedList -Encoding ascii
    rclone copy "$Remote/$Category" $categoryVerify --files-from $protectedList --retries 3 --contimeout 15s --timeout 5m
    if ($LASTEXITCODE -ne 0) { throw "$Category protected-set download failed" }
    if ($plan.protected.StartsWith("j2m-set-")) {
        python -m scripts.selfhost_backup verify-set (Join-Path $categoryVerify $plan.protected) --key-file $KeyFile
    } else {
        python -m scripts.selfhost_backup verify (Join-Path $categoryVerify $plan.protected) --key-file $KeyFile
    }
    if ($LASTEXITCODE -ne 0) { throw "$Category protected-set verification failed" }
    foreach ($relativePath in @($plan.delete)) {
        if ([string]::IsNullOrWhiteSpace($relativePath) -or $relativePath.Contains("/") -or $relativePath.Contains("\")) {
            throw "$Category retention produced an unsafe path"
        }
        rclone deletefile "$Remote/$Category/$relativePath" --retries 3 --contimeout 15s --timeout 5m
        if ($LASTEXITCODE -ne 0) { throw "$Category exact deletion failed" }
    }
}

try {
    $taskGuard = Enter-J2MSelfHostTaskGuard
    Send-J2MHealthcheck cloud_four_hour start
    if (-not (Get-Command rclone -ErrorAction SilentlyContinue)) { throw "rclone unavailable" }
    $root = Join-Path $Repository "selfhost-backups"
    $source = Join-Path $root "hourly"
    $outbound = Join-Path $root "outbound\four-hour"
    $verifyDir = Join-Path $root ("remote-verify-" + [guid]::NewGuid().ToString("N"))
    python -m scripts.selfhost_backup stage-offsite --source-dir $source --destination-dir $outbound --key-file $KeyFile --lock-file (Join-Path $root ".operations.lock")
    if ($LASTEXITCODE -ne 0) { throw "offsite staging failed" }
    $selectedItems = @(Get-ChildItem -LiteralPath $outbound -Filter "j2m-set-*.j2mbackup")
    if ($selectedItems.Count -gt 1) { throw "offsite staging produced multiple set indexes" }
    if ($selectedItems.Count -eq 1) { $selected = $selectedItems[0] } else {
        $legacy = @(Get-ChildItem -LiteralPath $outbound -Filter "*.j2mbackup")
        if ($legacy.Count -ne 1) { throw "offsite staging did not produce one recoverable set" }
        $selected = $legacy[0]
    }

    # Only one complete, locally authenticated bundle is staged for this four-hour slot.
    rclone copy $outbound "$Remote/four-hour" --immutable --retries 3 --contimeout 15s --timeout 5m
    if ($LASTEXITCODE -ne 0) { throw "cloud copy failed" }
    New-Item -ItemType Directory -Force -Path $verifyDir | Out-Null
    $allowlist = Join-Path $verifyDir "four-hour-files.txt"
    Get-ChildItem -LiteralPath $outbound -File | ForEach-Object { $_.Name } | Set-Content -LiteralPath $allowlist -Encoding ascii
    rclone copy "$Remote/four-hour" $verifyDir --files-from $allowlist --retries 3 --contimeout 15s --timeout 5m
    if ($LASTEXITCODE -ne 0) { throw "remote verification download failed" }
    if ($selected.Name.StartsWith("j2m-set-")) {
        python -m scripts.selfhost_backup verify-set (Join-Path $verifyDir $selected.Name) --key-file $KeyFile
    } else {
        python -m scripts.selfhost_backup verify (Join-Path $verifyDir $selected.Name) --key-file $KeyFile
    }
    if ($LASTEXITCODE -ne 0) { throw "remote cryptographic verification failed" }
    foreach ($category in @("daily", "configuration")) {
        rclone mkdir "$Remote/$category"
        if ($LASTEXITCODE -ne 0) { throw "$category remote directory preparation failed" }
    }

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
        foreach ($item in Get-ChildItem -LiteralPath $outbound -File) {
            rclone copyto $item.FullName "$Remote/daily/$($item.Name)" --immutable --retries 3 --contimeout 15s --timeout 5m
            if ($LASTEXITCODE -ne 0) { throw "daily copy failed" }
        }
        $dailyVerify = Join-Path $verifyDir "daily"
        New-Item -ItemType Directory -Force -Path $dailyVerify | Out-Null
        rclone copy "$Remote/daily" $dailyVerify --files-from $allowlist --retries 3 --contimeout 15s --timeout 5m
        if ($selected.Name.StartsWith("j2m-set-")) {
            python -m scripts.selfhost_backup verify-set (Join-Path $dailyVerify $selected.Name) --key-file $KeyFile
        } else {
            python -m scripts.selfhost_backup verify (Join-Path $dailyVerify $selected.Name) --key-file $KeyFile
        }
        if ($LASTEXITCODE -ne 0) { throw "daily cryptographic verification failed" }
    }

    # Each category inventories, authenticates and protects its own newest complete set.
    Invoke-ExactRetention "four-hour" 7
    Invoke-ExactRetention "daily" 30
    Invoke-ExactRetention "configuration" 30
    Remove-Item -LiteralPath $verifyDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $failure -Force -ErrorAction SilentlyContinue
    Send-J2MHealthcheck cloud_four_hour success
    $exitCode = 0
} catch {
    if ($null -ne $verifyDir) { Remove-Item -LiteralPath $verifyDir -Recurse -Force -ErrorAction SilentlyContinue }
    New-Item -ItemType Directory -Force -Path (Split-Path $failure) | Out-Null
    Set-Content -LiteralPath $failure -Value ((Get-Date).ToUniversalTime().ToString("o") + " cloud_copy_failed") -Encoding ascii
    Send-J2MHealthcheck cloud_four_hour fail
    $exitCode = 1
} finally {
    Exit-J2MSelfHostTaskGuard $taskGuard
}
exit $exitCode
